import asyncio
import aio_pika
import argparse
import os
import json
import urllib.parse
from datetime import datetime

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
RABBITMQ_MGMT_URL = os.environ.get("RABBITMQ_MGMT_URL", "http://rabbitmq:15672")
RABBITMQ_MGMT_USER = os.environ.get("RABBITMQ_USER", "")
RABBITMQ_MGMT_PASSWORD = os.environ.get("RABBITMQ_PASSWORD", "")

CANONICAL_DLQ_NAME = "dlq.failed"
REQUESTED_DLQ_NAME = (os.environ.get("DLQ_NAME") or CANONICAL_DLQ_NAME).strip() or CANONICAL_DLQ_NAME


def _require_credentials() -> None:
    missing = []
    if not RABBITMQ_URL:
        missing.append("RABBITMQ_URL")
    if not RABBITMQ_MGMT_USER:
        missing.append("RABBITMQ_USER")
    if not RABBITMQ_MGMT_PASSWORD:
        missing.append("RABBITMQ_PASSWORD")
    if missing:
        raise RuntimeError(f"Missing required environment variable(s): {', '.join(missing)}")


def _dlq_candidates() -> list[str]:
    requested = REQUESTED_DLQ_NAME
    if requested == CANONICAL_DLQ_NAME:
        return [CANONICAL_DLQ_NAME, "dead_letter_queue"]
    if requested == "dead_letter_queue":
        return ["dead_letter_queue", CANONICAL_DLQ_NAME]
    return [requested, CANONICAL_DLQ_NAME, "dead_letter_queue"]


async def resolve_dlq_name(channel):
    for queue_name in _dlq_candidates():
        try:
            await channel.declare_queue(queue_name, passive=True)
            return queue_name
        except Exception:
            continue

    print(f"No DLQ candidates found. Defaulting to '{CANONICAL_DLQ_NAME}'.")
    return CANONICAL_DLQ_NAME

async def get_dlq_messages(channel, dlq_name):
    queue = await channel.declare_queue(dlq_name, durable=True)
    messages = []
    
    # We can't easily browse the queue without consuming it in aio_pika
    # The dirty way is to consume them, store them, and immediately NACK to requeue
    # But for a management script, we'll fetch them, don't ack, then requeue them
    
    count = queue.declaration_result.message_count
    if count == 0:
        return []

    try:
        # Actually in production we should use RabbitMQ Management HTTP API to peek
        import httpx
        encoded_queue = urllib.parse.quote(dlq_name, safe="")
        res = httpx.post(
            f"{RABBITMQ_MGMT_URL}/api/queues/%2F/{encoded_queue}/get",
            auth=(RABBITMQ_MGMT_USER, RABBITMQ_MGMT_PASSWORD),
            json={"count": count, "ackmode": "ack_requeue_true", "encoding": "auto", "truncate": 50000}
        )
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"Error calling RabbitMQ API: {e}")
        
    return []

async def cmd_list():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        dlq_name = await resolve_dlq_name(channel)

        msgs = await get_dlq_messages(channel, dlq_name)
        if not msgs:
            print(f"DLQ '{dlq_name}' is empty.")
            return

        print(f"Queue: {dlq_name}")
        print(f"{'Idx':<4} | {'Routing Key':<20} | {'Error Reason':<30} | {'Time'}")
        print("-" * 80)
        for i, m in enumerate(msgs):
            headers = m.get("properties", {}).get("headers", {})
            x_death = headers.get("x-death", [{}])[0]
            reason = x_death.get("reason", "unknown")
            time_sec = x_death.get("time", {}).get("timestamp", 0)
            dt = datetime.fromtimestamp(time_sec).strftime('%Y-%m-%d %H:%M:%S') if time_sec else "unknown"
            r_key = m.get("routing_key", "unknown")
            print(f"{i:<4} | {r_key:<20} | {reason:<30} | {dt}")

async def cmd_stats():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        dlq_name = await resolve_dlq_name(channel)

        msgs = await get_dlq_messages(channel, dlq_name)
        if not msgs:
            print(f"DLQ '{dlq_name}' is empty.")
            return

        print(f"Queue: {dlq_name}")
        print(f"Total messages in DLQ: {len(msgs)}")

        reasons = {}
        oldest = float('inf')

        for m in msgs:
            headers = m.get("properties", {}).get("headers", {})
            x_death = headers.get("x-death", [{}])[0]
            reason = x_death.get("reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1

            time_sec = x_death.get("time", {}).get("timestamp", float('inf'))
            if time_sec < oldest:
                oldest = time_sec

        print("\nReasons breakdown:")
        for r, c in reasons.items():
            print(f"  - {r}: {c}")

        if oldest != float('inf'):
            dt = datetime.fromtimestamp(oldest).strftime('%Y-%m-%d %H:%M:%S')
            print(f"\nOldest message timestamp: {dt}")

async def cmd_export(filename):
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        dlq_name = await resolve_dlq_name(channel)

        msgs = await get_dlq_messages(channel, dlq_name)
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(msgs, f, indent=2)
        print(f"Exported {len(msgs)} messages from '{dlq_name}' to {filename}")

async def cmd_purge(confirm):
    if not confirm:
        c = input("Are you sure you want to PURGE the DLQ? (y/N): ")
        if c.lower() != 'y':
            print("Aborted.")
            return
            
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        dlq_name = await resolve_dlq_name(channel)
        queue = await channel.declare_queue(dlq_name, durable=True)
        await queue.purge()
        print(f"DLQ '{dlq_name}' purged successfully.")

async def cmd_retry_all():
    print("Retrying all messages from DLQ...")
    
    # Simple retry logic: Consume from DLQ and publish to original routing exchange
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        dlq_name = await resolve_dlq_name(channel)
        queue = await channel.declare_queue(dlq_name, durable=True)
        
        count = queue.declaration_result.message_count
        if count == 0:
            print(f"DLQ '{dlq_name}' is empty.")
            return
            
        retry_count = 0
        async with queue.iterator() as q_iter:
            async for message in q_iter:
                async with message.process():
                    # Find original routing key
                    headers = message.headers or {}
                    x_death = headers.get("x-death", [{}])[0]
                    orig_routing = x_death.get("routing-keys", ["unknown"])[0]
                    
                    if orig_routing == "unknown":
                        orig_routing = message.routing_key
                        
                    await channel.default_exchange.publish(
                        aio_pika.Message(
                            body=message.body,
                            headers=message.headers,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                        ),
                        routing_key=orig_routing
                    )
                    retry_count += 1
                    
                if retry_count >= count:
                    break
                    
        print(f"Successfully retried {retry_count} messages from '{dlq_name}'.")

async def main():
    _require_credentials()

    parser = argparse.ArgumentParser(description="Manage DLQ")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    subparsers.add_parser("list", help="List DLQ messages")
    subparsers.add_parser("stats", help="Show DLQ statistics")
    
    export_p = subparsers.add_parser("export", help="Export to JSON")
    export_p.add_argument("filename", help="Output file path")
    
    purge_p = subparsers.add_parser("purge", help="Purge the DLQ")
    purge_p.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    
    subparsers.add_parser("retry-all", help="Requeue all messages")
    
    args = parser.parse_args()
    
    if args.command == "list":
        await cmd_list()
    elif args.command == "stats":
        await cmd_stats()
    elif args.command == "export":
        await cmd_export(args.filename)
    elif args.command == "purge":
        await cmd_purge(args.confirm)
    elif args.command == "retry-all":
        await cmd_retry_all()

if __name__ == "__main__":
    asyncio.run(main())
