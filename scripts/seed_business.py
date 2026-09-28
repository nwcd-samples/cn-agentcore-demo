#!/usr/bin/env python3
"""往业务表写演示数据:3 个客户、6 个订单、若干工单。

订单刻意覆盖几种状态,保证 demo 的每条链路都有素材可演:
  ORD-1024  已发货但严重超期  -> 走物流查询 + 超时赔付计算 + 开工单
  ORD-1025  已签收            -> 正常回答,不该开工单
  ORD-1026  刚超期 3 小时      -> 不满 24 小时,赔付应为最低额,考验模型算得准不准
  ORD-1027  商品破损已开工单    -> 考验 list_tickets 去重
  ORD-1028  错发商品          -> 全额退款规则
  ORD-1029  待发货            -> 还没到承诺时间,不构成超期

时间是相对"现在"算的,所以每次跑都会刷新,不会因为放久了数据失真。

用法:
    export $(grep -v '^#' .env | xargs)
    python scripts/seed_business.py
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

HOUR = 3600
DAY = 24 * HOUR


def order_item(
    *,
    order_id: str,
    customer_id: str,
    status: str,
    amount_cents: int,
    created_offset: int,
    promised_offset: int,
    item_name: str,
    qty: int = 1,
    shipment_no: str = "",
    carrier: str = "",
    now: int,
) -> dict:
    """offset 为负数表示过去。promised_offset 为负即已过承诺时间。"""
    created_at = now + created_offset
    promised_at = now + promised_offset
    item: dict = {
        "PK": {"S": f"ORDER#{order_id}"},
        "SK": {"S": "META"},
        "GSI1PK": {"S": f"CUSTOMER#{customer_id}"},
        "GSI1SK": {"S": f"ORDER#{created_at:012d}#{order_id}"},
        "order_id": {"S": order_id},
        "customer_id": {"S": customer_id},
        "status": {"S": status},
        "amount_cents": {"N": str(amount_cents)},
        "currency": {"S": "CNY"},
        "item_name": {"S": item_name},
        "qty": {"N": str(qty)},
        "created_at": {"N": str(created_at)},
        "promised_at": {"N": str(promised_at)},
    }
    if shipment_no:
        item["shipment_no"] = {"S": shipment_no}
    if carrier:
        item["carrier"] = {"S": carrier}
    return item


def ticket_item(
    *, ticket_id: str, order_id: str, category: str, severity: str,
    summary: str, status: str, created_offset: int, now: int,
) -> dict:
    created_at = now + created_offset
    return {
        "PK": {"S": f"TICKET#{ticket_id}"},
        "SK": {"S": "META"},
        "GSI1PK": {"S": f"ORDER#{order_id}"},
        "GSI1SK": {"S": f"TICKET#{created_at:012d}#{ticket_id}"},
        "ticket_id": {"S": ticket_id},
        "order_id": {"S": order_id},
        "category": {"S": category},
        "severity": {"S": severity},
        "summary": {"S": summary},
        "status": {"S": status},
        "created_at": {"N": str(created_at)},
    }


def shipment_item(
    *,
    shipment_no: str,
    carrier: str,
    order_id: str,
    status: str,
    current_location: str,
    events: list[tuple[int, str, str]],
    now: int,
    stalled_hours: int | None = None,
) -> dict:
    """物流查询网页(src/lambdas/logistics)读的条目。

    events 里每项是 (相对 now 的秒偏移, 地点, 说明)。
    刻意把"异常"写成人类可读的文字而不是结构化字段 ——
    Agent 必须真的用浏览器读页面内容才能拿到,这才是 Browser 的演示点。
    """
    item: dict = {
        "PK": {"S": f"SHIPMENT#{shipment_no}"},
        "SK": {"S": "META"},
        "shipment_no": {"S": shipment_no},
        "carrier": {"S": carrier},
        "order_id": {"S": order_id},
        "status": {"S": status},
        "current_location": {"S": current_location},
        "events": {
            "L": [
                {
                    "M": {
                        "at": {"N": str(now + offset)},
                        "location": {"S": location},
                        "note": {"S": note},
                    }
                }
                for offset, location, note in events
            ]
        },
    }
    if stalled_hours is not None:
        item["stalled_hours"] = {"N": str(stalled_hours)}
    return item


def build_dataset(now: int) -> list[dict]:
    return [
        order_item(
            order_id="ORD-1024", customer_id="CUST-001", status="shipped",
            amount_cents=129900, created_offset=-9 * DAY, promised_offset=-3 * DAY - 5 * HOUR,
            item_name="人体工学办公椅", shipment_no="SF7758291046", carrier="顺丰",
            now=now,
        ),
        order_item(
            order_id="ORD-1025", customer_id="CUST-001", status="delivered",
            amount_cents=8900, created_offset=-20 * DAY, promised_offset=-17 * DAY,
            item_name="USB-C 数据线 2m", qty=2,
            shipment_no="YT4432018877", carrier="圆通", now=now,
        ),
        order_item(
            order_id="ORD-1026", customer_id="CUST-002", status="shipped",
            amount_cents=45600, created_offset=-4 * DAY, promised_offset=-3 * HOUR,
            item_name="机械键盘 87 键", shipment_no="JD9900112233", carrier="京东物流",
            now=now,
        ),
        order_item(
            order_id="ORD-1027", customer_id="CUST-002", status="delivered",
            amount_cents=239000, created_offset=-12 * DAY, promised_offset=-9 * DAY,
            item_name="27 寸 4K 显示器", shipment_no="SF7758300001", carrier="顺丰",
            now=now,
        ),
        order_item(
            order_id="ORD-1028", customer_id="CUST-003", status="delivered",
            amount_cents=59900, created_offset=-6 * DAY, promised_offset=-4 * DAY,
            item_name="无线鼠标", shipment_no="ZT8812349999", carrier="中通", now=now,
        ),
        order_item(
            order_id="ORD-1029", customer_id="CUST-003", status="pending",
            amount_cents=1599000, created_offset=-6 * HOUR, promised_offset=3 * DAY,
            item_name="升降办公桌 160x80", now=now,
        ),
        # ORD-1027 已有一张破损工单,用来验证模型开新工单前会先查重
        ticket_item(
            ticket_id="TKT-seed00000001", order_id="ORD-1027", category="damaged",
            severity="high", summary="显示器面板左下角有明显划痕,开箱即发现",
            status="open", created_offset=-8 * DAY, now=now,
        ),
        ticket_item(
            ticket_id="TKT-seed00000002", order_id="ORD-1028", category="wrong_item",
            severity="normal", summary="下单的是无线鼠标,实际收到有线款",
            status="resolved", created_offset=-3 * DAY, now=now,
        ),
        # ------------------------------------------------------------------
        # 物流轨迹。给自建的物流查询网页(Browser 的演示靶子)用。
        # 状态刻意和订单的 promised_at 对得上:Agent 从订单看到"超期 77 小时",
        # 从网页能看到"卡在中转中心 3 天",两边一致演示才可信。
        # ------------------------------------------------------------------
        shipment_item(
            shipment_no="SF7758291046", carrier="顺丰", order_id="ORD-1024",
            status="exception", current_location="陕西西安中转中心",
            stalled_hours=72,
            events=[
                (-9 * DAY + 2 * HOUR, "宁夏银兴仓", "快件已被揽收"),
                (-8 * DAY, "宁夏银川集散中心", "快件已到达"),
                (-8 * DAY + 6 * HOUR, "宁夏银川集散中心", "快件已发出,下一站陕西西安中转中心"),
                (-7 * DAY, "陕西西安中转中心", "快件已到达"),
                (-3 * DAY, "陕西西安中转中心", "因分拣设备故障,快件滞留待处理"),
                (-2 * HOUR, "陕西西安中转中心", "快件仍在等待转运,预计延迟 3 天以上"),
            ],
            now=now,
        ),
        shipment_item(
            shipment_no="YT4432018877", carrier="圆通", order_id="ORD-1025",
            status="delivered", current_location="已签收",
            events=[
                (-20 * DAY + 3 * HOUR, "广东深圳仓", "快件已被揽收"),
                (-19 * DAY, "湖北武汉中转中心", "快件已中转"),
                (-17 * DAY - 6 * HOUR, "宁夏银川金凤区网点", "快件已派送"),
                (-17 * DAY - 2 * HOUR, "宁夏银川金凤区", "快件已签收,签收人:本人"),
            ],
            now=now,
        ),
        shipment_item(
            shipment_no="JD9900112233", carrier="京东物流", order_id="ORD-1026",
            status="delayed", current_location="宁夏银川西夏区分拣中心",
            stalled_hours=5,
            events=[
                (-4 * DAY + HOUR, "北京亦庄仓", "快件已出库"),
                (-2 * DAY, "宁夏银川西夏区分拣中心", "快件已到达"),
                (-5 * HOUR, "宁夏银川西夏区分拣中心", "因当日运力紧张,派送延后"),
            ],
            now=now,
        ),
        shipment_item(
            shipment_no="SF7758300001", carrier="顺丰", order_id="ORD-1027",
            status="delivered", current_location="已签收",
            events=[
                (-12 * DAY + 4 * HOUR, "江苏苏州仓", "快件已被揽收"),
                (-10 * DAY, "陕西西安中转中心", "快件已中转"),
                (-9 * DAY - 3 * HOUR, "宁夏银川兴庆区网点", "快件已签收,签收人:前台代收"),
            ],
            now=now,
        ),
        shipment_item(
            shipment_no="ZT8812349999", carrier="中通", order_id="ORD-1028",
            status="delivered", current_location="已签收",
            events=[
                (-6 * DAY + 5 * HOUR, "浙江义乌仓", "快件已被揽收"),
                (-5 * DAY, "陕西西安中转中心", "快件已中转"),
                (-4 * DAY - HOUR, "宁夏银川金凤区", "快件已签收"),
            ],
            now=now,
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("PROJECT", "agentcore-cn"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-northwest-1"))
    parser.add_argument("--table", default=None, help="默认为 <project>-business")
    args = parser.parse_args()

    table = args.table or f"{args.project}-business"
    now = int(time.time())
    items = build_dataset(now)

    ddb = boto3.client("dynamodb", region_name=args.region)
    try:
        # 数据量小,逐条 put 比 batch 好读,失败也好定位
        for item in items:
            ddb.put_item(TableName=table, Item=item)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            print(f"找不到表 {table},先部署 00-foundation.yaml", file=sys.stderr)
        else:
            print(f"写入失败:{code}", file=sys.stderr)
        return 1

    orders = [i for i in items if i["PK"]["S"].startswith("ORDER#")]
    tickets = [i for i in items if i["PK"]["S"].startswith("TICKET#")]
    shipments = [i for i in items if i["PK"]["S"].startswith("SHIPMENT#")]
    print(
        f"已写入 {table}:{len(orders)} 个订单,{len(tickets)} 张工单,"
        f"{len(shipments)} 条物流轨迹\n"
    )
    print(f"{'订单号':<12}{'客户':<11}{'状态':<11}{'金额(元)':>10}  超期情况")
    for item in orders:
        promised = int(item["promised_at"]["N"])
        status = item["status"]["S"]
        overdue = now - promised
        if status == "delivered":
            note = "已签收"
        elif overdue > 0:
            note = f"已超期 {overdue / 3600:.1f} 小时"
        else:
            note = f"还有 {-overdue / 3600:.1f} 小时到期"
        print(
            f"{item['order_id']['S']:<12}{item['customer_id']['S']:<11}{status:<11}"
            f"{int(item['amount_cents']['N']) / 100:>10.2f}  {note}"
        )

    print(f"\n{'运单号':<16}{'承运商':<11}{'状态':<12}物流页可见信息")
    for item in shipments:
        stalled = item.get("stalled_hours", {}).get("N")
        note = f"已滞留 {stalled} 小时" if stalled else item["current_location"]["S"]
        print(
            f"{item['shipment_no']['S']:<16}{item['carrier']['S']:<11}"
            f"{item['status']['S']:<12}{note}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
