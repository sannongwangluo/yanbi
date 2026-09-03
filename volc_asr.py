"""volc_asr — 火山流式 ASR 协议实现（自包含模块，随本项目维护）。

火山引擎·豆包大模型流式语音识别（云端 WebSocket API）ASR 引擎。

背景（替换本地 whisper 的原因）：本地 tiny/base 模型对静音/噪声段幻觉严重，
会把"感谢观看"之类的训练数据套话当真文本输出。云端大模型 ASR 由服务端做语言模型
解码，静音/噪声会返回空音频错误码（45000002），不会幻觉出文本。

协议（火山方舟《接入语音模型》官方文档）：
- WebSocket 端点默认 `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async`
  （双向流式优化版），鉴权失败时回退 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`；
- 鉴权 HTTP header：X-Api-Key / X-Api-Resource-Id: volc.seedasr.sauc.duration /
  X-Api-Request-Id / X-Api-Connect-Id（随机 UUID）/ X-Api-Sequence: -1；
- 二进制帧（大端）：4 字节头（version<<4|header_size / message_type<<4|flags /
  serialization<<4|compression / 0x00）+ 可选 4 字节 sequence + 4 字节 payload size
  + gzip 压缩 payload。message_type：0b0001=full client request（JSON）、
  0b0010=audio only、0b1001=server full response、0b1111=error；
  flags：0b0001=正 seq、0b0011=负 seq（最后一包）；
- full client request 的 JSON：{"user":{"uid":"<uid>"},"audio":{"format":"pcm",
  "codec":"raw","rate":16000,"bits":16,"channel":1},"request":{"model_name":"bigmodel",
  "enable_itn":true,"enable_punc":true,"enable_ddc":true,"show_utterances":true}}；
- 音频按 200ms 分包（16kHz s16le mono = 6400 字节/包）。本模块是整段送（VAD 已切好句），
  快速发完即可，不做实时 sleep 模拟；最后一包用负 seq 标记；
- 服务端每包回 full server response，flags & 0x02 为最后一包；result_type 默认 full
  全量返回，取最后一包的 `result.text` 全文；error 帧解析错误码（45000002=空音频）记日志返回空串。

设计决策：
- 同步实现：用 `websockets.sync.client`（websockets v10+ 自带同步客户端），transcribe
  是同步接口，直接调用即可，避免 asyncio.run 与 ASR lane 线程的 event loop 纠缠；
- API 密钥只从环境变量读：优先 VOLC_API_KEY，fallback ARK_API_KEY；无 key 时引擎
  available=False，transcribe 返回空串，绝不抛异常（ASR lane 线程不能被炸）；
- 整段 transcribe 设硬超时（默认 15s），任何网络/协议异常都捕获、记日志、返回 ""；
- 纯协议函数（build_full_request / build_audio_request / parse_response / extract_text
  / frame_to_s16le / chunk_pcm）提为模块级，便于打真实网络 mock 验证编解码，
  不依赖真实 key。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

@dataclass
class AudioSegment:
    """一段检测到的语音片段。frame 是 float32 PCM（单声道），ts_ms 是片段起始时刻。"""

    frame: np.ndarray          # float32，单声道，[-1, 1]
    ts_ms: int                 # 片段起始时间戳（monotonic ms）
    sample_rate: int = 16000

logger = logging.getLogger(__name__)

# ---- 协议常量 ----

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0x01              # 1 word = 4 字节

MSG_FULL_REQUEST = 0b0001       # client full request（JSON）
MSG_AUDIO_ONLY = 0b0010         # client audio only
MSG_SERVER_FULL_RESPONSE = 0b1001
MSG_SERVER_ERROR = 0b1111

FLAG_NO_SEQ = 0b0000
FLAG_POS_SEQ = 0b0001           # 正 seq
FLAG_NEG_WITH_SEQ = 0b0011      # 负 seq（最后一包）

SERIALIZATION_NONE = 0b0000
SERIALIZATION_JSON = 0b0001

COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

# 端点选型（2026-08-26 实机对比）：本客户端是"整段录完再送"的批量语义，
# nostream（流式输入模式）等负包后整句通读出结果——长句标点正常、准确率更高、
# 还更快（21.9s 音频 3.3s）；bigmodel_async（双向流式）为边说边出字设计，
# 批量猛发时会按停顿疯狂分句，长句被插满句号（同音频 6.7s 且文本不可用），
# 仅留作兜底。/plan/ 变体对语音控制台 key 握手 401，不用。
PRIMARY_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
FALLBACK_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHUNK_MS = 200          # 200ms/包
DEFAULT_UID = "realtime-eye"

# 空音频错误码（服务端对纯静音段返回此码，非鉴权/网络错误，按空串处理）
EMPTY_AUDIO_CODE = 45000002

# ---- websockets 可选依赖（懒加载，失败不阻断 import）----
try:
    from websockets.sync.client import connect as _ws_connect
    from websockets.exceptions import ConnectionClosed as _ConnectionClosed
    from websockets.exceptions import InvalidStatus as _InvalidStatus
    _HAS_WEBSOCKETS = True
except Exception:  # pragma: no cover - websockets 缺失时降级为不可用
    _ws_connect = None
    _ConnectionClosed = Exception
    _InvalidStatus = Exception
    _HAS_WEBSOCKETS = False


@dataclass
class ServerResponse:
    """解析后的服务端帧（去掉二进制协议外壳，暴露语义字段）。"""

    message_type: int
    is_last: bool
    code: int
    data: Optional[dict]
    seq: Optional[int]
    event: Optional[int]


def build_header(message_type: int, flags: int,
                 serialization: int, compression: int) -> bytes:
    """4 字节协议头（大端）。

    byte0 = version<<4 | header_size（header_size 单位是 4 字节，恒 1）；
    byte1 = message_type<<4 | flags；byte2 = serialization<<4 | compression；byte3 = 0x00。
    """
    return bytes([
        (PROTOCOL_VERSION << 4) | HEADER_SIZE,
        (message_type << 4) | flags,
        (serialization << 4) | compression,
        0x00,
    ])


def build_full_request(seq: int, uid: str = DEFAULT_UID,
                       sample_rate: int = DEFAULT_SAMPLE_RATE,
                       hotwords: Optional[List[str]] = None) -> bytes:
    """构造 full client request（JSON payload，gzip 压缩，正 seq）。

    hotwords：热词直传（corpus.context，官方上限 100 tokens），专治专有名词
    识别（如人名/产品名/行话），等价于本地离线模型的 initial_prompt 预热词。
    """
    request = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "show_utterances": True,
    }
    if hotwords:
        request["corpus"] = {
            "context": json.dumps(
                {"hotwords": [{"word": w} for w in hotwords]},
                ensure_ascii=False)
        }
    payload = {
        "user": {"uid": uid},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": sample_rate,
            "bits": 16,
            "channel": 1,
        },
        "request": request,
    }
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return (
        build_header(MSG_FULL_REQUEST, FLAG_POS_SEQ,
                     SERIALIZATION_JSON, COMPRESSION_GZIP)
        + struct.pack(">i", seq)
        + struct.pack(">I", len(body))
        + body
    )


def build_audio_request(seq: int, pcm: bytes, is_last: bool) -> bytes:
    """构造 audio only request（gzip 压缩原始 PCM；最后一包用负 seq 标记）。"""
    flags = FLAG_NEG_WITH_SEQ if is_last else FLAG_POS_SEQ
    seq_val = -seq if is_last else seq
    body = gzip.compress(pcm)
    return (
        build_header(MSG_AUDIO_ONLY, flags,
                     SERIALIZATION_JSON, COMPRESSION_GZIP)
        + struct.pack(">i", seq_val)
        + struct.pack(">I", len(body))
        + body
    )


def parse_response(frame: bytes) -> Optional[ServerResponse]:
    """解析服务端二进制帧。协议不完整/解压失败返回 None（调用方跳过）。"""
    if not frame or len(frame) < 4:
        return None
    header_words = frame[0] & 0x0F
    message_type = frame[1] >> 4
    flags = frame[1] & 0x0F
    serialization = frame[2] >> 4
    compression = frame[2] & 0x0F
    offset = header_words * 4
    if len(frame) < offset:
        return None

    seq: Optional[int] = None
    if flags & 0x01:
        if len(frame) < offset + 4:
            return None
        seq = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
    is_last = bool(flags & 0x02)
    event: Optional[int] = None
    if flags & 0x04:
        if len(frame) < offset + 4:
            return None
        event = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4

    code = 0
    if message_type == MSG_SERVER_ERROR:
        if len(frame) < offset + 8:
            return None
        code = struct.unpack(">i", frame[offset:offset + 4])[0]
        offset += 4
        size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
    elif message_type == MSG_SERVER_FULL_RESPONSE:
        if len(frame) < offset + 4:
            return None
        size = struct.unpack(">I", frame[offset:offset + 4])[0]
        offset += 4
    else:
        return ServerResponse(message_type, is_last, code, None, seq, event)

    payload = frame[offset:offset + size]
    if compression == COMPRESSION_GZIP and payload:
        try:
            payload = gzip.decompress(payload)
        except Exception:
            payload = b""
    data: Optional[dict] = None
    if payload and serialization == SERIALIZATION_JSON:
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception:
            data = None
    return ServerResponse(message_type, is_last, code, data, seq, event)


def extract_text(data: Optional[dict]) -> str:
    """从 full response 的 JSON payload 提取 `result.text` 全文（兜底顶层 text）。"""
    if not isinstance(data, dict):
        return ""
    result = data.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            return text.strip()
    text = data.get("text")
    return text.strip() if isinstance(text, str) else ""


def frame_to_s16le(frame: np.ndarray) -> bytes:
    """float32 单声道 PCM（[-1,1]，16k）→ 16bit 小端 PCM 字节。"""
    a = np.asarray(frame, dtype=np.float32)
    if a.size == 0:
        return b""
    a = np.clip(a, -1.0, 1.0)
    return (a * 32767.0).astype(np.int16).tobytes()


def chunk_pcm(pcm: bytes, chunk_bytes: int = DEFAULT_SAMPLE_RATE * 2 * DEFAULT_CHUNK_MS // 1000) -> List[bytes]:
    """把一段 PCM 字节按 chunk_bytes 切片（非空输入至少返回 1 块）。"""
    if not pcm:
        return []
    return [pcm[i:i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]


class VolcASR:
    """火山引擎豆包大模型流式语音识别 ASR（云端 WebSocket）。

    - API 密钥从环境变量读：优先 VOLC_API_KEY，fallback ARK_API_KEY；构造参数
      api_key 可显式覆盖（测试注入用）。注意 openspeech（豆包语音）与方舟 LLM
      是两套鉴权：本机 ARK_API_KEY 是方舟 LLM key，openspeech 会 401 拒收
      （实机验证错误码 45000010），正确的 key 需在火山引擎「语音技术」控制台
      创建语音应用、开通大模型流式语音识别后获取，故 VOLC_API_KEY 优先。
    - available：有 key 且 websockets 可用才 True；无 key 时 _error 给出中文申请指引。
    - transcribe(segment)：同步、永不抛异常；任何网络/协议/超时异常都捕获记日志
      返回空串。静音/噪声段服务端返回空音频错误码（45000002），同样返回空串——
      这正是替换本地离线模型的目的：静音绝不幻觉出文本。
    """

    name = "volc"

    def __init__(self, api_key: Optional[str] = None,
                 url: Optional[str] = None,
                 timeout: float = 15.0,
                 resource_id: str = DEFAULT_RESOURCE_ID,
                 sample_rate: int = DEFAULT_SAMPLE_RATE,
                 chunk_ms: int = DEFAULT_CHUNK_MS,
                 uid: str = DEFAULT_UID,
                 hotwords: Optional[List[str]] = None) -> None:
        # 优先级见类 docstring：VOLC_API_KEY 优先，ARK_API_KEY 兜底。
        self.api_key = api_key or os.environ.get("VOLC_API_KEY") or os.environ.get("ARK_API_KEY")
        self.url = url or PRIMARY_URL
        self.timeout = timeout
        self.resource_id = resource_id
        self.sample_rate = sample_rate
        self.chunk_bytes = int(sample_rate * 2 * chunk_ms / 1000)
        self.uid = uid
        self.hotwords = list(hotwords) if hotwords else None
        self._error = ""
        if not self.api_key:
            self._error = (
                "未配置火山引擎 API 密钥：请在环境变量 VOLC_API_KEY 中设置"
                "（申请入口：火山引擎「语音技术」控制台 → 创建应用 → 开通"
                "大模型流式语音识别 → 获取 API Key），或构造时传 api_key=..."
            )

    @property
    def available(self) -> bool:
        return bool(self.api_key) and _HAS_WEBSOCKETS

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": self.api_key or "",
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Connect-Id": str(uuid.uuid4()),
            "X-Api-Sequence": "-1",
        }

    def _candidate_urls(self) -> List[str]:
        if self.url == PRIMARY_URL:
            return [PRIMARY_URL, FALLBACK_URL]
        return [self.url]

    def transcribe(self, segment: AudioSegment) -> str:
        if not self.api_key:
            logger.error("volc ASR 未初始化：%s", self._error)
            return ""
        if not _HAS_WEBSOCKETS:
            logger.error("volc ASR 依赖 websockets 未安装，转写不可用")
            return ""
        try:
            pcm = frame_to_s16le(getattr(segment, "frame", None))
            if not pcm:
                return ""
            chunks = chunk_pcm(pcm, self.chunk_bytes)
            if not chunks:
                return ""
            return self._run(chunks)
        except Exception as e:  # 绝不让异常抛进 ASR lane 线程
            logger.error("volc ASR 转写异常：%s", e, exc_info=True)
            return ""

    def _run(self, chunks: List[bytes]) -> str:
        """按候选端点依次尝试连接；连接/握手失败才换端点，协议级错误直接返回空串。"""
        last_err: Optional[Exception] = None
        for url in self._candidate_urls():
            try:
                return self._run_once(url, chunks)
            except _InvalidStatus as e:
                last_err = e
                status = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning("volc ASR 端点 %s 握手被拒（HTTP %s，疑似鉴权失败），尝试下一端点", url, status)
            except Exception as e:
                last_err = e
                logger.warning("volc ASR 端点 %s 连接失败：%s", url, e)
        logger.error("volc ASR 所有端点连接失败：%s", last_err)
        return ""

    def _run_once(self, url: str, chunks: List[bytes]) -> str:
        """单端点：握手 → 发 full request → 快速发完音频分包 → 读响应取全文。"""
        with _ws_connect(url, additional_headers=self._headers(),
                         open_timeout=self.timeout,
                         close_timeout=min(5.0, self.timeout),
                         max_queue=None) as ws:
            ws.send(build_full_request(1, uid=self.uid, sample_rate=self.sample_rate,
                                       hotwords=self.hotwords))
            seq = 2
            n = len(chunks)
            for i, chunk in enumerate(chunks):
                ws.send(build_audio_request(seq, chunk, is_last=(i == n - 1)))
                seq += 1

            deadline = time.monotonic() + self.timeout
            final_text = ""
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("volc ASR 转写超时（%.1fs）", self.timeout)
                    break
                try:
                    msg = ws.recv(timeout=remaining)
                except _ConnectionClosed:
                    break
                except TimeoutError:
                    logger.warning("volc ASR 等待响应超时（%.1fs）", self.timeout)
                    break
                except Exception as e:
                    logger.warning("volc ASR 读取响应异常：%s", e)
                    break
                if isinstance(msg, str):
                    continue  # 忽略文本帧
                resp = parse_response(msg)
                if resp is None:
                    continue
                if resp.code != 0:
                    logger.error("volc ASR 服务端错误码 %d（payload=%r）", resp.code, resp.data)
                    return ""
                text = extract_text(resp.data)
                if text:
                    final_text = text
                if resp.is_last:
                    break
            return final_text.strip()
