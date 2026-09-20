"""时间处理工具：统一时区、解析带时区的时间、按本地自然日切片。

业务约定：
- 事件必须携带时区偏移（山区设备离线补传时以设备记录的发生时间为准）；
- 内部一律换算为 UTC 存储与比较；
- “哪一天”的归属按东八区自然日计算，跨午夜作业在午夜处切片。
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8), name="UTC+8")


def parse_dt(value: object) -> datetime:
    """解析 ISO 8601 时间为 UTC 时间；缺少时区偏移时拒绝，避免歧义。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间必须是非空字符串")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区偏移（如 +08:00 或 Z）")
    return parsed.astimezone(timezone.utc)


def to_local(moment: datetime) -> datetime:
    """把 UTC 时间换算到业务本地时区。"""

    return moment.astimezone(LOCAL_TZ)


def day_key(moment: datetime) -> str:
    """返回时刻所属的本地自然日（YYYY-MM-DD）。"""

    return to_local(moment).date().isoformat()


def slice_by_day(start: datetime, end: datetime) -> list[tuple[str, int]]:
    """把 [start, end) 区间按本地自然日切片，返回 (日期, 分钟数) 列表。

    跨午夜作业因此在两天各记一段，结算与申诉都能按日核对。
    """

    if end <= start:
        return []
    slices: list[tuple[str, int]] = []
    cursor = start
    while cursor < end:
        local = to_local(cursor)
        midnight_local = datetime.combine(
            local.date() + timedelta(days=1), time.min, tzinfo=LOCAL_TZ
        )
        boundary = midnight_local.astimezone(timezone.utc)
        seg_end = min(boundary, end)
        minutes = int((seg_end - cursor).total_seconds() // 60)
        if minutes > 0:
            slices.append((day_key(cursor), minutes))
        cursor = seg_end
    return slices
