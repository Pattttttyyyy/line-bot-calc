"""Provider-independent LINE image/command correlation, not wired to live app.

Only signed, verified webhook events may be passed to this module. The store is
an injected durable metadata repository, not an in-process dict in production.
No QR contents, login credentials or image bytes are stored by this module.
"""
from dataclasses import dataclass
import math
from .flow import Refused, parse_points


@dataclass(frozen=True)
class ImageRef:
    group: str
    operator: str
    message: str
    received_at: float  # Signed LINE event timestamp, not replay receipt time.


@dataclass(frozen=True)
class CommandRef:
    group: str
    operator: str
    message: str
    points: int
    image_message: str
    sent_at: float


def authorize(group, operator, *, admin, enabled=False, group_allowed=False):
    if admin is not True:
        raise Refused('只有管理員可以扣點。')
    if enabled is not True:
        raise Refused('自動扣點尚未啟用。')
    if not isinstance(group, str) or not group.startswith('group:') or not operator or group_allowed is not True:
        raise Refused('請在已設定的群組操作。')


def valid_time(timestamp, now):
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (timestamp, now)):
        raise Refused('圖片或指令時間不正確。')
    if not 0 <= now-timestamp <= 300:
        raise Refused('圖片或指令已過期，請重新貼圖。')


def receive_image(image, store, *, now, **policy):
    authorize(image.group,image.operator,**policy)
    if not image.message:
        raise Refused('缺少圖片訊息編號。')
    valid_time(image.received_at,now)
    # save_once must deduplicate image ID and preserve original timestamp.
    # latest ordering must use signed event time; replay cannot replace newer QR.
    store.save_once(image)
    return '已收到圖片；請在5分鐘內輸入扣點3290（點數依本筆交易）。'


def prepare_command(text, group, operator, message, sent_at, store, *, now, **policy):
    authorize(group,operator,**policy)
    points=parse_points(text)
    if not message:
        raise Refused('缺少指令訊息編號。')
    valid_time(sent_at,now)
    image=store.latest(group,operator)
    if image is None:
        raise Refused('請先由你在本群貼QR圖，再輸入扣點指令。')
    if image.group!=group or image.operator!=operator:
        raise Refused('圖片不屬於本群或此管理員。')
    valid_time(image.received_at,now)
    if image.received_at>sent_at:
        raise Refused('請貼圖後重新輸入扣點指令。')
    # The caller must bind this immutable selection to message ID in the durable
    # payment journal BEFORE querying/confirming. Do not reselect on replay.
    return CommandRef(group,operator,message,points,image.message,sent_at)
