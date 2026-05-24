import amqplib from 'amqplib';
import Redis from 'ioredis';
import pino from 'pino';
import { randomUUID } from 'crypto';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export class BrokerProducer {
    private brokerType: string;
    private rmqConn: any = null;
    private rmqChannel: any = null;
    private redis: Redis | null = null;
    private isConnected: boolean = false;
    private isReconnecting: boolean = false;
    private amqpUrl: string;
    private redisUrl: string;

    constructor() {
        this.brokerType = process.env.BROKER_TYPE || 'rabbitmq';
        this.amqpUrl = (process.env.RABBITMQ_URL || '').trim();
        this.redisUrl = (process.env.REDIS_URL || '').trim();
    }

    async connect(baseDelayForRetry = 2000, maxRetries = 20) {
        if (this.brokerType === 'none') {
            this.isConnected = true;
            logger.info('Broker type is none, bypassing connection');
            return;
        }

        if (this.brokerType === 'rabbitmq' && !this.amqpUrl) {
            throw new Error('RABBITMQ_URL is required when BROKER_TYPE=rabbitmq');
        }
        if (this.brokerType === 'redis' && !this.redisUrl) {
            throw new Error('REDIS_URL is required when BROKER_TYPE=redis');
        }

        let attempt = 0;
        
        while (attempt < maxRetries) {
            try {
                if (this.brokerType === 'rabbitmq') {
                    this.rmqConn = await amqplib.connect(this.amqpUrl, {
                        heartbeat: 10,
                        timeout: 5000,
                    });
                    this.rmqChannel = await this.rmqConn.createConfirmChannel();

                    this.rmqConn.on('error', (err: any) => {
                        logger.error({ err }, 'RabbitMQ connection error');
                        this.isConnected = false;
                        this.scheduleReconnect();
                    });

                    this.rmqConn.on('close', () => {
                        logger.warn('RabbitMQ connection closed, will auto-reconnect');
                        this.isConnected = false;
                        this.scheduleReconnect();
                    });

                    // Ensure exchange exists
                    await this.rmqChannel.assertExchange('whatsapp.events', 'topic', { durable: true });
                } else if (this.brokerType === 'redis') {
                    this.redis = new Redis(this.redisUrl, {
                        maxRetriesPerRequest: 1,
                        enableReadyCheck: true,
                        lazyConnect: true,
                    });
                    this.redis.on('error', (err: any) => {
                        logger.error({ err }, 'Redis connection error');
                        this.isConnected = false;
                    });
                    await this.redis.connect();
                } else {
                    throw new Error(`Unsupported BROKER_TYPE: ${this.brokerType}`);
                }

                this.isConnected = true;
                this.isReconnecting = false;
                logger.info(`Connected to broker: ${this.brokerType}`);
                return;
            } catch (err) {
                attempt++;
                // Capped exponential backoff at 30 seconds
                const delay = Math.min(baseDelayForRetry * Math.pow(1.5, attempt - 1), 30000);
                
                if (attempt >= maxRetries) {
                    logger.error(`Failed to connect after ${maxRetries} attempts. Giving up.`);
                    throw new Error(`Could not connect to broker after ${maxRetries} attempts`);
                }
                
                logger.warn(`Failed to connect to broker (attempt ${attempt}/${maxRetries}): ${err}. Retrying in ${Math.round(delay / 1000)}s...`);
                await new Promise(resolve => setTimeout(resolve, delay));
            }
        }
    }

    private scheduleReconnect() {
        if (this.isReconnecting) return;
        this.isReconnecting = true;
        logger.info('Scheduling broker reconnect in 5s...');
        setTimeout(async () => {
            try {
                await this.connect();
            } catch (err) {
                logger.error({ err }, 'Failed to reconnect to broker');
                this.isReconnecting = false;
            }
        }, 5000);
    }

    private async ensureConnected(): Promise<void> {
        if (this.isConnected) return;
        logger.warn('Broker not connected, attempting reconnect before publish...');
        await this.connect();
    }

    private lastPublishTime: number = 0;
    private readonly minInterval: number = 1000; // Max 1 message per second (1000ms)

    private getRedisStreamsForRoutingKey(routingKey: string): string[] {
        if (routingKey.startsWith('msg.media.')) {
            return ['messages.inbound', 'media.download'];
        }

        if (routingKey === 'msg.text') {
            return ['messages.inbound'];
        }

        if (routingKey === 'history.sync' || routingKey === 'messages.history') {
            return ['messages.history'];
        }

        if (routingKey === 'msg.status') {
            return ['messages.status'];
        }

        if (routingKey === 'contacts.update') {
            return ['contacts.update'];
        }

        if (routingKey === 'findings.publish') {
            return ['findings.publish'];
        }

        if (routingKey === 'session.heartbeat' || routingKey === 'session.status') {
            return ['session.events'];
        }

        if (routingKey === 'groups.update' || routingKey === 'groups.participants.update') {
            return ['groups.metadata'];
        }

        if (routingKey === 'profile_photo.process') {
            return ['media.profile_photo'];
        }

        return [routingKey];
    }

    private shouldThrottle(routingKey: string): boolean {
        // Only throttle user-facing WhatsApp send operations — not internal broker events.
        // messages.history is an internal broker publish (not a WA API call) and can be
        // large volume during initial bootstrap, so it must NOT be throttled.
        return routingKey.startsWith('msg.') || routingKey.startsWith('profile_photo.');
    }

    async publish(routingKey: string, message: any): Promise<void> {
        await this.ensureConnected();

        // Anti-ban throttling: Only apply to user-facing WhatsApp operations
        if (this.shouldThrottle(routingKey)) {
            const now = Date.now();
            const timeSinceLast = now - this.lastPublishTime;
            if (timeSinceLast < this.minInterval) {
                const waitTime = this.minInterval - timeSinceLast;
                const jitter = Math.floor(Math.random() * 500);
                await new Promise(resolve => setTimeout(resolve, waitTime + jitter));
            }
            this.lastPublishTime = Date.now();
        }

        const sessionName = process.env.SESSION_NAME || 'default';

        // ... rest of the existing publish method ...
        const enrichedMessage = {
            ...message,
            _metadata: {
                timestamp: new Date().toISOString(),
                correlation_id: message?._metadata?.correlation_id || message?.correlation_id || randomUUID(),
                session_name: sessionName
            }
        };

        const payload = Buffer.from(JSON.stringify(enrichedMessage));

        if (this.brokerType === 'rabbitmq' && this.rmqChannel) {
            return new Promise((resolve, reject) => {
                this.rmqChannel!.publish('whatsapp.events', routingKey, payload, { persistent: true }, (err: any) => {
                    if (err) {
                        logger.error({ err, routingKey, msgId: message.message_id }, 'Failed to publish to RabbitMQ');
                        reject(err);
                    } else {
                        logger.debug({ routingKey, msgId: message.message_id }, 'Published to RabbitMQ successfully');
                        resolve();
                    }
                });
            });
        } else if (this.brokerType === 'redis' && this.redis) {
            try {
                const streams = this.getRedisStreamsForRoutingKey(routingKey);
                for (const stream of streams) {
                    await this.redis.xadd(
                        stream,
                        '*',
                        'payload',
                        payload.toString('utf-8'),
                        'routing_key',
                        routingKey
                    );
                }
                logger.debug({ routingKey, streams, msgId: message.message_id }, 'Published to Redis Stream successfully');
            } catch (err) {
                logger.error({ err, routingKey, msgId: message.message_id }, 'Failed to publish to Redis');
                throw err;
            }
        }
    }

    async flush(): Promise<void> {
        logger.info('Flushing broker producer...');
        if (this.brokerType === 'rabbitmq' && this.rmqChannel) {
            await this.rmqChannel.waitForConfirms();
            await this.rmqChannel.close();
            await this.rmqConn?.close();
        } else if (this.brokerType === 'redis' && this.redis) {
            await this.redis.quit();
        }
        this.isConnected = false;
        logger.info('Broker producer flushed and closed');
    }
}

export const producer = new BrokerProducer();
