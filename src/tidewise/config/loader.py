"""YAML 配置加载。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from tidewise.config.schema import AppConfig

_SECRET_KEY_RE = re.compile(r"password|secret|auth_?code|token", re.IGNORECASE)


class ConfigError(RuntimeError):
    pass


def _find_secret_keys(node: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if _SECRET_KEY_RE.search(str(k)):
                hits.append(p)
            hits.extend(_find_secret_keys(v, p))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_find_secret_keys(v, f"{path}[{i}]"))
    return hits


def parse_config(raw: Any, source: str = "<dict>") -> AppConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: 顶层必须是映射")
    secret_keys = _find_secret_keys(raw)
    if secret_keys:
        raise ConfigError(
            f"{source}: 配置文件不得包含密钥字段 {secret_keys}，请改用 TIDEWISE_* 环境变量"
        )
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"{source}: 配置校验失败\n{e}") from e


def load_config(path: str | Path) -> AppConfig:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"无法读取配置文件 {p}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: YAML 解析失败: {e}") from e
    return parse_config(raw, source=str(p))
