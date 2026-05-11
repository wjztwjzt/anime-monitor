"""接码 API 响应若包含下列子串，视为死号/冻结（与 check_dead_accounts 共用）。"""

ACCOUNT_DEAD_OR_FROZEN_KEYWORDS: tuple[str, ...] = (
    "账号死亡",
    "冻结",
)


def body_indicates_dead_or_frozen(body: str) -> list[str]:
    if not body:
        return []
    return [kw for kw in ACCOUNT_DEAD_OR_FROZEN_KEYWORDS if kw in body]
