"""企业微信/微信公众号 回调加密模式加解密工具.

企业微信与公众号的安全模式回调共用同一种加解密方案：
- AES-256-CBC 加密
- key = base64.b64decode(aes_key + "=")，PKCS7 padding
- 明文格式: random(16字节) + msg_len(4字节网络序) + msg + receiveid

参考: https://developer.work.weixin.qq.com/document/path/90968
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import struct

logger = logging.getLogger(__name__)


def _aes_key(aes_key: str) -> bytes:
    return base64.b64decode(aes_key + "=")


def decrypt_wecom_message(encrypt: str, aes_key: str, receiveid: str = "") -> str:
    """解密企业微信/公众号回调密文.

    Args:
        encrypt: 密文 (Base64)
        aes_key: 配置的 EncodingAESKey
        receiveid: 接收方标识（企业微信为 CorpID，公众号为 AppID）

    Returns:
        解密后的明文 XML；失败返回空字符串。
    """
    try:
        key = _aes_key(aes_key)
        cipher = _cipher_obj(key)
        raw = cipher.decrypt(base64.b64decode(encrypt))
        raw = _pkcs7_unpad(raw)
        # 前 16 字节随机串，接着 4 字节网络序长度，之后是消息，最后 receiveid
        msg_len = struct.unpack(">I", raw[16:20])[0]
        msg = raw[20 : 20 + msg_len].decode("utf-8")
        return msg
    except Exception as e:  # noqa: BLE001
        logger.error(f"企业微信回调解密失败: {e}")
        return ""


def encrypt_wecom_reply(xml: str, aes_key: str, receiveid: str = "") -> str:
    """加密企业微信/公众号回复内容.

    Returns:
        Base64 密文；失败返回空字符串。
    """
    try:
        key = _aes_key(aes_key)
        random16 = os.urandom(16)
        msg_bytes = xml.encode("utf-8")
        msg_len = struct.pack(">I", len(msg_bytes))
        receiveid_bytes = receiveid.encode("utf-8")
        raw = random16 + msg_len + msg_bytes + receiveid_bytes
        raw = _pkcs7_pad(raw)
        cipher = _cipher_obj(key)
        return base64.b64encode(cipher.encrypt(raw)).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        logger.error(f"企业微信回调加密失败: {e}")
        return ""


def verify_signature(token: str, signature: str, timestamp: str, nonce: str, encrypt: str = "") -> bool:
    """验证企业微信回调签名 msg_signature."""
    if not signature:
        return False
    arr = sorted([token, timestamp, nonce, encrypt]) if encrypt else sorted([token, timestamp, nonce])
    sign = hashlib.sha1("".join(arr).encode("utf-8")).hexdigest()
    return sign == signature


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32:
        return data
    return data[:-pad_len]


def _cipher_obj(key: bytes):
    """构建 AES-CBC 密文对象 (pycryptodome 或 cryptography 兼容)."""
    try:
        from Crypto.Cipher import AES

        return AES.new(key, AES.MODE_CBC, key[:16])
    except ImportError:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def _new_dec():
            cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
            return cipher.decryptor()

        def _new_enc():
            cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
            return cipher.encryptor()

        class _CompatAES:
            def encrypt(self, data: bytes) -> bytes:
                enc = _new_enc()
                return enc.update(data) + enc.finalize()

            def decrypt(self, data: bytes) -> bytes:
                dec = _new_dec()
                return dec.update(data) + dec.finalize()

        return _CompatAES()
