"""账号密码加解密模块。

提供独立的密码加密与解密能力，集中管理密钥推导与算法实现，
便于单独替换或升级加解密方案，不影响其他业务代码。

设计目标：
- 数据库中不存储明文密码；攻击者仅拿到数据库与解密后的算法结果，
  在没有源代码/密钥的情况下难以直接还原原始密码。
- 不采用 Hash，因为业务需要解密后使用明文密码登录站点。
- 主密钥应通过环境变量 `ZIBLLCRAWLER_ENCRYPTION_KEY` 提供；
  未提供时使用内置默认密钥（仅用于本地测试，生产环境必须配置强密钥）。

当前实现：基于 PBKDF2 + Fernet（AES-128-CBC + HMAC-SHA256）。
"""

import base64
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


ENCRYPTED_PREFIX = "enc:"
# 固定盐用于保证同一主密钥在不同运行实例间产生相同的派生密钥。
# 安全前提是主密钥足够强且不被泄露。
_STATIC_SALT = b"zibllcrawler-v1-salt"
_PBKDF2_ITERATIONS = 260_000


class PasswordCrypto:
    """账号密码加解密器。"""

    def __init__(self, master_key: Optional[str] = None):
        self.logger = logging.getLogger("zibllcrawler.core.password_crypto")
        key_source = master_key or os.environ.get("ZIBLLCRAWLER_ENCRYPTION_KEY")

        if not key_source:
            self.logger.warning(
                "未设置 ZIBLLCRAWLER_ENCRYPTION_KEY，使用内置默认主密钥。"
                "生产环境必须设置强随机主密钥。"
            )
            key_source = "__zibllcrawler_default_master_key__"

        self._fernet = self._derive_fernet(key_source)

    @staticmethod
    def _derive_fernet(master_key: str) -> Fernet:
        """使用 PBKDF2 从主密钥派生 Fernet 密钥。"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_STATIC_SALT,
            iterations=_PBKDF2_ITERATIONS,
            backend=default_backend(),
        )
        derived_key = base64.urlsafe_b64encode(kdf.derive(master_key.encode("utf-8")))
        return Fernet(derived_key)

    def encrypt(self, plaintext: str) -> str:
        """加密明文密码，返回带前缀的字符串。"""
        if plaintext.startswith(ENCRYPTED_PREFIX):
            self.logger.debug("密码已带加密前缀，跳过重复加密")
            return plaintext
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return f"{ENCRYPTED_PREFIX}{token}"

    def decrypt(self, ciphertext: str) -> str:
        """解密密码；若输入为明文则原样返回。"""
        if not ciphertext.startswith(ENCRYPTED_PREFIX):
            self.logger.debug("密码未加密，直接返回")
            return ciphertext

        token = ciphertext[len(ENCRYPTED_PREFIX):]
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            self.logger.error("密码解密失败，主密钥可能不匹配")
            raise ValueError("密码解密失败，请检查 ZIBLLCRAWLER_ENCRYPTION_KEY") from exc

    def reencrypt(self, ciphertext: str) -> str:
        """对可能是明文或密文的输入进行加密，确保输出为密文。"""
        return self.encrypt(self.decrypt(ciphertext))


def create_crypto(master_key: Optional[str] = None) -> PasswordCrypto:
    """工厂函数：创建 PasswordCrypto 实例。"""
    return PasswordCrypto(master_key=master_key)
