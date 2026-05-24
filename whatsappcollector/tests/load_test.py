import asyncio
import aio_pika
import argparse
import json
import uuid
import random
import time
import os

RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")


async def connect_with_retry(url: str, retries: int = 8, base_delay_seconds: float = 1.0):
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return await aio_pika.connect_robust(url)
        except Exception as exc:  # pragma: no cover - exercised in real infra startup races
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = min(base_delay_seconds * (2 ** (attempt - 1)), 10.0)
            print(
                f"RabbitMQ connect attempt {attempt}/{retries} failed: {exc}. "
                f"Retrying in {wait_seconds:.1f}s..."
            )
            await asyncio.sleep(wait_seconds)
    raise RuntimeError(f"Unable to connect to RabbitMQ at {url} after {retries} attempts: {last_error}")

async def generate_message():
    # Mix: 60% text, 20% image, 10% video, 5% audio, 5% document
    rand = random.random()
    if rand < 0.60:
        msg_type = "text"
        routing_key = "msg.text"
        body = f"Synthetic load test message {uuid.uuid4()}"
        media = None
    elif rand < 0.80:
        msg_type = "image"
        routing_key = "msg.media.image"
        body = "Here is a test image"
        media = {
            "mimetype": "image/jpeg",
            "url": "https://example.com/test.jpg",
            "directPath": "/test/path/img.jpg",
            "fileLength": 102400
        }
    elif rand < 0.90:
        msg_type = "video"
        routing_key = "msg.media.video"
        body = "Here is a test video"
        media = {
            "mimetype": "video/mp4",
            "url": "https://example.com/test.mp4",
            "directPath": "/test/path/vid.mp4",
            "fileLength": 1024000
        }
    elif rand < 0.95:
        msg_type = "audio"
        routing_key = "msg.media.audio"
        body = ""
        media = {
            "mimetype": "audio/ogg; codecs=opus",
            "url": "https://example.com/test.ogg",
            "directPath": "/test/path/aud.ogg",
            "fileLength": 51200
        }
    else:
        msg_type = "document"
        routing_key = "msg.media.document"
        body = "document.pdf"
        media = {
            "mimetype": "application/pdf",
            "url": "https://example.com/test.pdf",
            "directPath": "/test/path/doc.pdf",
            "fileLength": 204800
        }

    payload = {
        "message_id": f"LOADTEST_{uuid.uuid4()}",
        "chat_jid": "1234567890@s.whatsapp.net",
        "sender_jid": "0987654321@s.whatsapp.net",
        "timestamp": int(time.time()),
        "message_type": msg_type,
        "body": body,
        "is_forwarded": False,
        "forwarding_score": 0,
        "media_metadata": media
    }
    
    return routing_key, payload

async def publisher(target_count=10000, batch_size=100, delay=0.1, connect_retries=8):
    print(f"Connecting to {RABBITMQ_URL}...")
    connection = await connect_with_retry(RABBITMQ_URL, retries=connect_retries)
    
    async with connection:
        channel = await connection.channel()
        exchange = await channel.declare_exchange("whatsapp.events", aio_pika.ExchangeType.TOPIC, durable=True)
        
        print(f"Starting load test generation: target={target_count} messages")
        start_time = time.time()
        published = 0
        
        while published < target_count:
            tasks = []
            for _ in range(min(batch_size, target_count - published)):
                r_key, payload = await generate_message()
                msg = aio_pika.Message(
                    body=json.dumps(payload).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT
                )
                tasks.append(exchange.publish(msg, routing_key=r_key))
            
            await asyncio.gather(*tasks)
            published += len(tasks)
            print(f"Published {published}/{target_count} messages... ({(time.time() - start_time):.2f}s elapsed)")
            await asyncio.sleep(delay)
            
        total_time = time.time() - start_time
        throughput = published / total_time if total_time > 0 else 0.0
        print(f"Load test completed! Published {published} messages in {total_time:.2f} seconds.")
        print(f"Throughput: {throughput:.2f} msg/sec")

        return {
            "published": published,
            "elapsed_seconds": round(total_time, 4),
            "throughput_msg_per_sec": round(throughput, 4),
            "batch_size": batch_size,
            "delay_seconds": delay,
        }


def evaluate_thresholds(stats: dict, max_total_seconds: float | None, min_throughput: float | None) -> list[str]:
    failures: list[str] = []

    if max_total_seconds is not None and stats["elapsed_seconds"] > max_total_seconds:
        failures.append(
            f"Elapsed time {stats['elapsed_seconds']:.2f}s exceeded max {max_total_seconds:.2f}s"
        )

    if min_throughput is not None and stats["throughput_msg_per_sec"] < min_throughput:
        failures.append(
            f"Throughput {stats['throughput_msg_per_sec']:.2f} msg/sec below min {min_throughput:.2f} msg/sec"
        )

    return failures


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded RabbitMQ load smoke generator and gate")
    parser.add_argument("--target-count", type=int, default=10000, help="Total messages to publish")
    parser.add_argument("--batch-size", type=int, default=200, help="Messages per publish batch")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between batches in seconds")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible message mix")
    parser.add_argument("--connect-retries", type=int, default=8, help="RabbitMQ connection retry attempts")
    parser.add_argument("--max-total-seconds", type=float, default=None, help="Fail if elapsed runtime exceeds this")
    parser.add_argument("--min-throughput", type=float, default=None, help="Fail if msg/sec is below this")
    return parser

if __name__ == "__main__":
    args = build_arg_parser().parse_args()
    random.seed(args.seed)

    stats = asyncio.run(
        publisher(
            target_count=args.target_count,
            batch_size=args.batch_size,
            delay=args.delay,
            connect_retries=args.connect_retries,
        )
    )

    print("LOAD_SMOKE_SUMMARY", json.dumps(stats, sort_keys=True))

    threshold_failures = evaluate_thresholds(
        stats,
        max_total_seconds=args.max_total_seconds,
        min_throughput=args.min_throughput,
    )

    if threshold_failures:
        print("LOAD_SMOKE_GATE_FAILED")
        for failure in threshold_failures:
            print(f" - {failure}")
        raise SystemExit(1)

    print("LOAD_SMOKE_GATE_PASSED")
