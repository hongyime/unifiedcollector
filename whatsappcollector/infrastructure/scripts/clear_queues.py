import asyncio
import aio_pika
import argparse
import os

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
CANONICAL_DLQ_QUEUE = "dlq.failed"


def normalize_queue_name(queue_name: str) -> str:
    name = (queue_name or "").strip()
    if name == "dead_letter_queue":
        return CANONICAL_DLQ_QUEUE
    return name

async def get_queue_depth(channel, queue_name):
    # Declare passively to get message count without creating if it doesn't exist
    try:
        queue = await channel.declare_queue(queue_name, passive=True)
        return queue.declaration_result.message_count
    except Exception:
        return 0

async def purge_queue(channel, queue_name):
    queue = await channel.declare_queue(queue_name, durable=True)
    await queue.purge()

async def main():
    parser = argparse.ArgumentParser(description="Manage RabbitMQ queues")
    parser.add_argument("--queue", help="Specific queue to drain")
    parser.add_argument("--all", action="store_true", help="Drain all standard queues")
    parser.add_argument("--dry-run", action="store_true", help="Show depths without draining")
    args = parser.parse_args()

    # Pre-defined known queues based on our infrastructure
    known_queues = [
        "messages.inbound",
        "messages.history",
        "messages.status",
        "contacts.update",
        "groups.metadata",
        "session.events",
        "calls.inbound",
        "findings.publish",
        CANONICAL_DLQ_QUEUE,
    ]

    target_queues = []
    if args.queue:
        target_queues.append(normalize_queue_name(args.queue))
    elif args.all:
        target_queues = known_queues
    else:
        print("Please specify --queue <name> or --all. Use --help for more info.")
        return

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()

        # Handle dry-run
        if args.dry_run:
            print(f"{'Queue Name':<20} | {'Depth'}")
            print("-" * 30)
            for q in target_queues:
                depth = await get_queue_depth(channel, q)
                print(f"{q:<20} | {depth}")
            return

        # Handle purge
        if args.all:
            confirm = input("Are you sure you want to drain ALL queues? (y/N): ")
            if confirm.lower() != 'y':
                print("Aborted.")
                return

        for q in target_queues:
            depth = await get_queue_depth(channel, q)
            if depth > 0:
                print(f"Purging {q} ({depth} messages)...")
                await purge_queue(channel, q)
            else:
                print(f"{q} is already empty.")

        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
