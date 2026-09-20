"""隐私与权限：位置最小化、角色门禁与访问审计。

约定：
- 精确坐标只用于当场换算，落库前降为约 1 公里网格（保留两位小数）；
- 服务内不保存姓名、证件号等身份资料，村民一律以工号引用；
- 涉及个人数据的读取都会写入审计日志，供隐私审计员核查。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ForbiddenError, UnauthorizedError, ValidationError

ROLES = ("leader", "base_admin", "reviewer", "finance", "auditor", "worker")
ROLE_LABELS = {
    "leader": "带班人",
    "base_admin": "基地管理员",
    "reviewer": "联盟复核员",
    "finance": "结算员",
    "auditor": "隐私审计员",
    "worker": "务工村民",
}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str


def parse_actor(actor_id: object, role: object) -> Actor:
    """从请求头解析操作者；缺少身份或角色未知时拒绝。"""

    if not actor_id or not isinstance(actor_id, str):
        raise UnauthorizedError("缺少 X-Actor-Id 请求头")
    if role not in ROLES:
        raise UnauthorizedError(f"未知或未提供的角色: {role!r}")
    return Actor(actor_id=actor_id, role=role)


def require(actor: Actor, allowed: set[str]) -> None:
    """角色门禁：只向职责范围内的角色开放。"""

    if actor.role not in allowed:
        allowed_labels = "、".join(ROLE_LABELS[item] for item in sorted(allowed))
        raise ForbiddenError(
            f"角色 {ROLE_LABELS[actor.role]} 无权执行该操作（需要：{allowed_labels}）"
        )


def to_cell(lat: object, lon: object) -> str:
    """把精确坐标降为约 1 公里网格；原始坐标不落库。"""

    try:
        flat = float(lat)  # type: ignore[arg-type]
        flon = float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValidationError("位置必须包含数值型 lat/lon") from exc
    if not (-90 <= flat <= 90 and -180 <= flon <= 180):
        raise ValidationError("位置坐标超出合理范围")
    return f"{flat:.2f},{flon:.2f}"
