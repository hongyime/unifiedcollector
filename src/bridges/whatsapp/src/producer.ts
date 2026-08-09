// RabbitMQ producer for the unifiedcollector WhatsApp bridge.
// Publishes normalized events to the durable topic exchange 'whatsapp.events'.
// The Python whatsapp collector binds a queue with routing key 'messages.#',
// so message + history events MUST use 'messages.*' routing keys.

import amqplib from 'amqplib';
import pino from 'pino';
import { randomUUID } from 'crypto';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

const EXCHANGE = 'whatsapp.events';

export class BrokerProducer {
    private amqpUrl: string;
    private conn: any = null;
    private channel: any = null;
    private connected = false;
    private reconnecting = false;

    constructor() {
        // Bridge reads WHATSAPP_RABBITMQ_URL first (matches the Python collector's
        // env), then falls back to RABBITMQ_URL.
        this.amqpUrl = (process.env.WHATSAPP_RABBITMQ_URL || process.env.RABBITMQ_URL || '').trim();
    }

    async connect(baseDelay = 2000, maxRetries = 30): Promise<void> {
        if (!this.amqpUrl) {
            throw new Error('WHATSAPP_RABBITMQ_URL (or RABBITMQ_URL) is required');
        }
        let attempt = 0;
        while (attempt < maxRetries) {
            try {
                this.conn = await amqplib.connect(this.amqpUrl, { heartbeat: 10, timeout: 5000 } as any);
                this.channel = await this.conn.createConfirmChannel();

                this.conn.on('error', (err: any) => {
                    logger.error({ err }, 'RabbitMQ connection error');
                    this.connected = false;
                    this.scheduleReconnect();
                });
                this.conn.on('close', () => {
                    logger.warn('RabbitMQ connection closed, will auto-reconnect');
                    this.connected = false;
                    this.scheduleReconnect();
                });

                await this.channel.assertExchange(EXCHANGE, 'topic', { durable: true });

                this.connected = true;
                this.reconnecting = false;
                logger.info('Connected to RabbitMQ; exchange %s asserted', EXCHANGE);
                return;
            } catch (err) {
                attempt++;
                const delay = Math.min(baseDelay * Math.pow(1.5, attempt - 1), 30000);
                if (attempt >= maxRetries) {
                    logger.error('Failed to connect to RabbitMQ after %d attempts', maxRetries);
                    throw err;
                }
                logger.warn('RabbitMQ connect attempt %d/%d failed: %s. Retrying in %ds',
                    attempt, maxRetries, err, Math.round(delay / 1000));
                await new Promise((r) => setTimeout(r, delay));
            }
        }
    }

    private scheduleReconnect(): void {
        if (this.reconnecting) return;
        this.reconnecting = true;
        setTimeout(async () => {
            try {
                await this.connect();
            } catch (err) {
                logger.error({ err }, 'RabbitMQ reconnect failed');
                this.reconnecting = false;
            }
        }, 5000);
    }

    private async ensureConnected(): Promise<void> {
        if (this.connected) return;
        logger.warn('Broker not connected; reconnecting before publish');
        await this.connect();
    }

    async publish(routingKey: string, message: any): Promise<void> {
        await this.ensureConnected();

        const sessionName = process.env.SESSION_NAME || 'default';
        const enriched = {
            ...message,
            session_name: message?.session_name || sessionName,
            _metadata: {
                timestamp: new Date().toISOString(),
                correlation_id: message?._metadata?.correlation_id || message?.correlation_id || randomUUID(),
                session_name: sessionName,
            },
        };
        const payload = Buffer.from(JSON.stringify(enriched));

        return new Promise<void>((resolve, reject) => {
            this.channel.publish(EXCHANGE, routingKey, payload, { persistent: true }, (err: any) => {
                if (err) {
                    logger.error({ err, routingKey }, 'Publish failed');
                    reject(err);
                } else {
                    logger.debug({ routingKey }, 'Published');
                    resolve();
                }
            });
        });
    }

    async flush(): Promise<void> {
        try {
            if (this.channel) {
                await this.channel.waitForConfirms();
                await this.channel.close();
            }
            await this.conn?.close();
        } catch (err) {
            logger.error({ err }, 'Error flushing producer');
        }
        this.connected = false;
    }
}

export const producer = new BrokerProducer();
