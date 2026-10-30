"""Cookie 支持:读取用户自备的 cookie 文件 + 加密存储。

**边界(与 kiana 一致)**:cookie 只用于让平台把"匿名可见"的内容按登录态返回,
不用于绕过账号权限;拿不到就降级并给出可读原因,不强行绕过。

* ``--cookies <file>`` 直接使用用户自备的 Netscape 格式 cookie 文件;
* :class:`CookieStore` 把 cookie 文件用 **Fernet(AES-128-CBC+HMAC)** 加密后落盘,
  密钥来自环境变量 ``MELAB_COOKIE_KEY``(未设置则回退 ``MELAB_KEY``),
  **密钥与明文都不进日志、不进导出产物**。
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

#: cookie 密钥的环境变量(优先)
COOKIE_KEY_ENV = "MELAB_COOKIE_KEY"

#: 备用密钥环境变量
FALLBACK_KEY_ENV = "MELAB_KEY"


class CookieError(RuntimeError):
    """cookie 相关错误(缺文件 / 缺密钥 / 解密失败)。"""


def cookie_key() -> bytes | None:
    """取 cookie 加密密钥(未配置返回 ``None``)。

    :return: Fernet 可用的 32 字节密钥;未配置返回 ``None``。
    """
    material = os.environ.get(COOKIE_KEY_ENV) or os.environ.get(FALLBACK_KEY_ENV)
    if not material:
        return None
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def validate_cookie_file(path: str | os.PathLike[str]) -> str:
    """校验用户自备 cookie 文件可用。

    :param path: cookie 文件路径。
    :return: 绝对路径。
    :raises CookieError: 文件不存在或为空。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise CookieError(f"cookie 文件不存在: {target}")
    if target.stat().st_size == 0:
        raise CookieError(f"cookie 文件为空: {target}")
    return str(target.resolve())


class CookieStore:
    """加密的 cookie 文件仓库。"""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        """初始化仓库。

        :param root: 存放目录(一般是 ``<output>/accounts``)。
        """
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._key = cookie_key()

    @property
    def enabled(self) -> bool:
        """是否具备加密能力(有密钥)。"""
        return self._key is not None

    def _fernet(self) -> object:
        """构造 Fernet 实例(无密钥时抛错)。"""
        if self._key is None:
            raise CookieError(
                f"未配置 cookie 加密密钥:请设置环境变量 {COOKIE_KEY_ENV}"
            )
        from cryptography.fernet import Fernet  # 第三方库:Fernet 对称加密

        return Fernet(self._key)

    def save(self, name: str, source: str | os.PathLike[str]) -> str:
        """导入一个 cookie 文件并加密保存。

        :param name: 账号名(作为文件名)。
        :param source: 源 cookie 文件路径。
        :return: 加密文件的路径。
        """
        plain = Path(validate_cookie_file(source)).read_bytes()
        token = self._fernet().encrypt(plain)  # type: ignore[attr-defined]
        target = self.root / f"{_safe(name)}.enc"
        target.write_bytes(token)
        return str(target)

    def load(self, name: str) -> str:
        """解密一个已保存的 cookie 文件,返回**临时明文文件**路径。

        明文只落在系统临时目录,用完由调用方删除;不写进项目目录。

        :param name: 账号名。
        :return: 明文 cookie 文件路径。
        """
        import tempfile

        source = self.root / f"{_safe(name)}.enc"
        if not source.is_file():
            raise CookieError(f"未找到已保存的 cookie: {name}")
        plain = self._fernet().decrypt(source.read_bytes())  # type: ignore[attr-defined]
        handle = tempfile.NamedTemporaryFile(
            prefix="melab-cookie-", suffix=".txt", delete=False
        )
        handle.write(plain)
        handle.close()
        return handle.name

    def names(self) -> list[str]:
        """列出已保存的账号名。"""
        return sorted(p.stem for p in self.root.glob("*.enc"))


def cookie_header(path: str | os.PathLike[str]) -> str:
    """把 Netscape 格式 cookie 文件转成 ``Cookie`` 请求头。

    :param path: cookie 文件路径。
    :return: ``name=value; name2=value2`` 形式的请求头值;没有 cookie 返回空串。
    """
    import http.cookiejar

    jar = http.cookiejar.MozillaCookieJar(str(validate_cookie_file(path)))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        raise CookieError(f"cookie 文件解析失败: {exc!r}") from exc
    pairs = [f"{cookie.name}={cookie.value}" for cookie in jar]
    return "; ".join(pairs)


def _safe(name: str) -> str:
    """把账号名转成安全的文件名。"""
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("-", "_", "."))
    return cleaned or "default"


def describe_cookie_source(cookies: str | None) -> str:
    """生成可安全展示的 cookie 来源描述(不含任何 cookie 内容)。

    :param cookies: cookie 文件路径。
    :return: 描述文本。
    """
    if not cookies:
        return "匿名(无 cookie)"
    return f"用户自备 cookie({Path(cookies).name})"
