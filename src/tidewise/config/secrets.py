"""密钥只从环境变量读取；repr/str 一律脱敏。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ENV_PREFIX = "TIDEWISE_"


class MissingSecretError(RuntimeError):
    pass


class Secret:
    """包装敏感字符串，防止误打进日志。取值必须显式调用 reveal()。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('******')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def _require(env: Mapping[str, str], name: str) -> Secret:
    key = ENV_PREFIX + name
    value = env.get(key, "").strip()
    if not value:
        raise MissingSecretError(f"缺少环境变量 {key}")
    return Secret(value)


@dataclass(frozen=True)
class CtpCredentials:
    user_id: Secret
    password: Secret
    auth_code: Secret

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CtpCredentials:
        env = os.environ if env is None else env
        return cls(
            user_id=_require(env, "CTP_USER_ID"),
            password=_require(env, "CTP_PASSWORD"),
            auth_code=_require(env, "CTP_AUTH_CODE"),
        )


@dataclass(frozen=True)
class FeishuCredentials:
    app_id: Secret
    app_secret: Secret

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FeishuCredentials:
        env = os.environ if env is None else env
        return cls(
            app_id=_require(env, "FEISHU_APP_ID"),
            app_secret=_require(env, "FEISHU_APP_SECRET"),
        )
