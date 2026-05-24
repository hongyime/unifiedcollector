import { WASocket } from '@whiskeysockets/baileys';
import pino from 'pino';
import amqplib from 'amqplib';
import Redis from 'ioredis';
import fs from 'fs';

const logger = pino({ level: process.env.LOG_LEVEL || 'info' });

export class FindingsHubSender {
    private sock: WASocket | null = null;
    private rmqConn: any = null;
    private rmqChannel: any = null;
    private redis: Redis | null = null;
    private hubGroupJid: string | null = null;
    private enabled: boolean = true;
    private isRunning: boolean = false;
    private lastSendTime: number = 0;
    private rateLimitMs: number = 5000;
    private brokerType: string;
    private redisGroup: string;
    private redisConsumer: string;

    constructor() {
        this.enabled = process.env.FINDINGS_HUB_ENABLED !== 'false';
        this.brokerType = process.env.BROKER_TYPE || 'rabbitmq';
        this.redisGroup = 'findings_hub';
        this.redisConsumer = `hub-${process.env.SESSION_NAME || 'default'}`;
    }

    get hubStatus(): 'active' | 'disabled' | 'group_not_found' {
        if (!this.enabled) return 'disabled';
        if (!this.isRunning && !this.hubGroupJid) return 'group_not_found';
        return 'active';
    }

    public async start(sock: WASocket) {
        if (!this.enabled) {
            logger.info('Findings Hub features are disabled via env (FINDINGS_HUB_ENABLED=false)');
            return;
        }

        if (this.isRunning) {
            logger.debug('Findings Hub is already running, skipping start');
            return;
        }

        this.sock = sock;
        this.isRunning = true;
        logger.info({ brokerType: this.brokerType }, 'Findings Hub starting');

        try {
            await this.detectHubGroup();
        } catch (error) {
            logger.error({ err: error }, 'Failed to detect Findings Hub group');
            this.isRunning = false;
            return;
        }

        if (!this.hubGroupJid) {
            const configuredName = process.env.FINDINGS_HUB_GROUP_NAME || 'Your Findings Hub Group';
            logger.error(
                {
                    token: 'FINDINGS_HUB_NOT_FOUND',
                    configuredGroupName: configuredName,
                    fix: 'Set FINDINGS_HUB_GROUP_JID=<jid> in your .env file to bypass group detection'
                },
                `FINDINGS_HUB_NOT_FOUND: Could not find WhatsApp group "${configuredName}". ` +
                `Set FINDINGS_HUB_GROUP_JID=<jid> in .env to fix this.`
            );
            await this._alertGroupNotFound();
            this.isRunning = false;
            return;
        }

        await this.connectBroker();
        await this.startConsumer();
    }

    public async stop() {
        this.isRunning = false;
        try { if (this.rmqChannel) await this.rmqChannel.close(); } catch (_) {}
        try { if (this.rmqConn) await this.rmqConn.close(); } catch (_) {}
        try { if (this.redis) await this.redis.quit(); } catch (_) {}
    }

    private async _publishHubConfiguredEvent(jid: string, groupName: string): Promise<void> {
        const payload = {
            event_type: 'findings_hub_configured',
            session_name: process.env.SESSION_NAME || '',
            jid,
            group_name: groupName,
        };
        try {
            if (this.brokerType === 'rabbitmq') {
                const amqpUrl = (process.env.RABBITMQ_URL || '').trim();
                if (!amqpUrl) return;
                const conn = await amqplib.connect(amqpUrl);
                const ch = await conn.createChannel();
                ch.sendToQueue('session.events', Buffer.from(JSON.stringify(payload)), { persistent: true });
                await ch.close();
                await conn.close();
            } else if (this.brokerType === 'redis') {
                const redisUrl = (process.env.REDIS_URL || '').trim();
                if (!redisUrl) return;
                const r = new Redis(redisUrl);
                await r.xadd('session.events', '*',
                    'payload', JSON.stringify(payload),
                    'routing_key', 'session.events'
                );
                await r.quit();
            }
            logger.info({ jid, groupName }, 'findings_hub_configured_event_published');
        } catch (err) {
            logger.warn({ err }, 'Failed to publish findings_hub_configured event (best-effort)');
        }
    }

    private async _alertGroupNotFound(): Promise<void> {
        const configuredName = process.env.FINDINGS_HUB_GROUP_NAME || 'Your Findings Hub Group';
        const caption =
            `⚠️ FINDINGS_HUB_NOT_FOUND\n` +
            `Group "${configuredName}" not found.\n` +
            `Set FINDINGS_HUB_GROUP_JID=<jid> in .env to fix this.`;

        try {
            if (this.brokerType === 'rabbitmq') {
                const amqpUrl = (process.env.RABBITMQ_URL || '').trim();
                if (!amqpUrl) {
                    logger.warn('Cannot publish group_not_found alert: RABBITMQ_URL not set');
                    return;
                }
                const conn = await amqplib.connect(amqpUrl);
                const ch = await conn.createChannel();
                await ch.checkQueue('findings.publish');
                ch.sendToQueue('findings.publish', Buffer.from(JSON.stringify({
                    identity_id: '00000000-0000-0000-0000-000000000000',
                    original_image_path: '',
                    caption,
                    event_type: 'system_alert'
                })), { persistent: true });
                await ch.close();
                await conn.close();
            } else if (this.brokerType === 'redis') {
                const redisUrl = (process.env.REDIS_URL || '').trim();
                if (!redisUrl) {
                    logger.warn('Cannot publish group_not_found alert: REDIS_URL not set');
                    return;
                }
                const r = new Redis(redisUrl);
                await r.xadd('findings.publish', '*',
                    'payload', JSON.stringify({
                        identity_id: '00000000-0000-0000-0000-000000000000',
                        original_image_path: '',
                        caption,
                        event_type: 'system_alert'
                    }),
                    'routing_key', 'findings.publish'
                );
                await r.quit();
            }
        } catch (e) {
            logger.warn({ err: e }, 'Could not publish group_not_found alert to broker (best-effort)');
        }
    }

    private async detectHubGroup() {
        const targetGroupName = process.env.FINDINGS_HUB_GROUP_NAME || 'Your Findings Hub Group';
        const overrideJid = process.env.FINDINGS_HUB_GROUP_JID;
        const JID_CACHE_PATH = '/app/auth_info/findings_hub_jid.txt';

        if (overrideJid) {
            this.hubGroupJid = overrideJid;
            logger.info(`Using manual Findings Hub JID override: ${overrideJid}`);
            await this._publishHubConfiguredEvent(overrideJid, 'override');
            return;
        }

        if (fs.existsSync(JID_CACHE_PATH)) {
            try {
                const cachedJid = fs.readFileSync(JID_CACHE_PATH, 'utf8').trim();
                if (cachedJid) {
                    this.hubGroupJid = cachedJid;
                    logger.info({ jid: cachedJid }, 'findings_hub_jid_loaded_from_cache');
                    return;
                }
            } catch (err) {
                logger.warn({ err }, 'Failed to read findings hub JID cache, proceeding with API detection');
            }
        }

        try {
            const groups = await this.sock!.groupFetchAllParticipating();
            const groupList = Object.values(groups);

            // Publish all groups to groups.metadata so collector can persist them
            try {
                if (this.brokerType === 'rabbitmq') {
                    const amqpUrl = (process.env.RABBITMQ_URL || '').trim();
                    if (amqpUrl) {
                        const conn = await amqplib.connect(amqpUrl);
                        const ch = await conn.createChannel();
                        for (const g of groupList) {
                            ch.sendToQueue('groups.metadata', Buffer.from(JSON.stringify({
                                id: g.id,
                                jid: g.id,
                                subject: g.subject,
                                chat_type: 'group',
                                member_count: g.participants?.length ?? 0,
                            })), { persistent: true });
                        }
                        await ch.close();
                        await conn.close();
                        logger.info({ count: groupList.length }, 'groups_metadata_published');
                    }
                } else if (this.brokerType === 'redis') {
                    const redisUrl = (process.env.REDIS_URL || '').trim();
                    if (redisUrl) {
                        const r = new Redis(redisUrl);
                        for (const g of groupList) {
                            await r.xadd('groups.metadata', '*',
                                'payload', JSON.stringify({
                                    id: g.id,
                                    jid: g.id,
                                    subject: g.subject,
                                    chat_type: 'group',
                                    member_count: g.participants?.length ?? 0,
                                }),
                                'routing_key', 'groups.metadata'
                            );
                        }
                        await r.quit();
                        logger.info({ count: groupList.length }, 'groups_metadata_published');
                    }
                }
            } catch (publishErr) {
                logger.warn({ err: publishErr }, 'Failed to publish groups to broker (best-effort)');
            }

            const hubGroup = groupList.find(g => g.subject === targetGroupName);

            if (hubGroup) {
                this.hubGroupJid = hubGroup.id;
                logger.info(`Detected Findings Hub Group: ${hubGroup.subject} (${hubGroup.id})`);
                // Print JID prominently for easy copy-paste to .env
                logger.warn(`\n${'='.repeat(80)}\n💡 FINDINGS HUB GROUP JID DETECTED:\n   Group: ${hubGroup.subject}\n   JID: ${hubGroup.id}\n\n   Add this to your .env file:\n   FINDINGS_HUB_GROUP_JID=${hubGroup.id}\n${'='.repeat(80)}\n`);
                try {
                    fs.writeFileSync(JID_CACHE_PATH, hubGroup.id, 'utf8');
                } catch (writeErr) {
                    logger.warn({ err: writeErr }, 'Failed to write findings hub JID cache');
                }
                await this._publishHubConfiguredEvent(hubGroup.id, hubGroup.subject);
            } else {
                logger.warn(`Could not find group with name: "${targetGroupName}"`);
            }
        } catch (error) {
            logger.error({ err: error }, 'Error fetching participing groups for Findings Hub detection');
        }
    }

    private async connectBroker() {
        if (this.brokerType === 'redis') {
            const redisUrl = (process.env.REDIS_URL || '').trim();
            if (!redisUrl) {
                throw new Error('REDIS_URL is required when BROKER_TYPE=redis');
            }
            try {
                this.redis = new Redis(redisUrl, {
                    maxRetriesPerRequest: 1,
                    enableReadyCheck: true,
                    lazyConnect: true
                });
                await this.redis.connect();
                try {
                    await this.redis.xgroup('CREATE', 'findings.publish', this.redisGroup, '$', 'MKSTREAM');
                } catch (err: any) {
                    if (!String(err?.message || '').includes('BUSYGROUP')) {
                        throw err;
                    }
                }
                logger.info({ redisUrl }, 'Findings Hub connected to Redis');
            } catch (error) {
                logger.error({ err: error }, 'Findings Hub Redis connection failed');
                throw error;
            }
            return;
        }

        const amqpUrl = (process.env.RABBITMQ_URL || '').trim();
        if (!amqpUrl) {
            throw new Error('RABBITMQ_URL is required when BROKER_TYPE=rabbitmq');
        }
        let retries = 5;

        while (retries > 0) {
            try {
                this.rmqConn = await amqplib.connect(amqpUrl);
                this.rmqChannel = await this.rmqConn.createChannel();

                this.rmqConn.on('error', (err: any) => {
                    logger.error({ err }, 'FindingsHub RabbitMQ connection error');
                    this.stop();
                });

                // Try asserting with correct DLQ args first
                try {
                    await this.rmqChannel.assertQueue('findings.publish', {
                        durable: true,
                        arguments: {
                            'x-dead-letter-exchange': 'dlq.events',
                            'x-dead-letter-routing-key': 'dlq.failed'
                        }
                    });
                } catch (assertErr: any) {
                    if (assertErr?.code === 406) {
                        // PRECONDITION_FAILED — queue exists with different args.
                        // The 406 kills the channel, so reconnect it and just verify the queue exists.
                        logger.warn('Queue findings.publish already exists with different args, using existing queue');
                        this.rmqChannel = await this.rmqConn.createChannel();
                        await this.rmqChannel.checkQueue('findings.publish');
                    } else {
                        throw assertErr;
                    }
                }

                logger.info('Findings Hub connected to RabbitMQ');
                return;
            } catch (error) {
                retries--;
                logger.warn(`Failed to connect Findings Hub to RabbitMQ. Retries left: ${retries}`);
                if (retries === 0) throw error;
                await new Promise(r => setTimeout(r, 2000));
            }
        }
    }

    private async startConsumer() {
        if (this.brokerType === 'redis') {
            if (!this.redis || !this.isRunning) return;
            while (this.isRunning) {
                try {
                    const result = await this.redis.xreadgroup(
                        'GROUP',
                        this.redisGroup,
                        this.redisConsumer,
                        'COUNT',
                        1,
                        'BLOCK',
                        5000,
                        'STREAMS',
                        'findings.publish',
                        '>'
                    );

                    if (!result) continue;
                    for (const [, messages] of result as any) {
                        for (const [messageId, fields] of messages) {
                            const payloadStr = fields?.payload || '';
                            try {
                                const payload = JSON.parse(payloadStr);
                                if (this.hubGroupJid) {
                                    await this.processMessage(payload);
                                }
                                await this.redis.xack('findings.publish', this.redisGroup, messageId);
                            } catch (error) {
                                logger.error({ err: error, messageId }, 'Failed to process findings message');
                                await this.redis.xack('findings.publish', this.redisGroup, messageId);
                                await this.redis.xadd(
                                    'dlq.failed',
                                    '*',
                                    'payload',
                                    payloadStr,
                                    'routing_key',
                                    fields?.routing_key || 'findings.publish'
                                );
                            }
                        }
                    }
                } catch (error) {
                    logger.error({ err: error, consumer: this.redisConsumer }, 'Findings Hub Redis consume error');
                }
            }
            return;
        }

        if (!this.rmqChannel || !this.isRunning) return;

        // Ensure we don't process too fast by limiting prefetch to 1
        await this.rmqChannel.prefetch(1);

        this.rmqChannel.consume('findings.publish', async (msg: any) => {
            if (!msg) return;

            try {
                const payload = JSON.parse(msg.content.toString());

                if (this.hubGroupJid) {
                    await this.processMessage(payload);
                } else {
                    logger.debug('Discarding findings message because hubGroupJid is not set.');
                }

                this.rmqChannel!.ack(msg);
            } catch (error) {
                logger.error({ err: error }, 'Failed to process findings message');
                // Nack but do not requeue to avoid infinite loops on poison messages
                this.rmqChannel!.nack(msg, false, false);
            }
        });
    }

    private async processMessage(payload: any) {
        const now = Date.now();
        const timeSinceLast = now - this.lastSendTime;

        if (timeSinceLast < this.rateLimitMs) {
            const waitTime = this.rateLimitMs - timeSinceLast;
            await new Promise(r => setTimeout(r, waitTime));
        }

        const { identity_id, original_image_path, caption } = payload;

        try {
            if (fs.existsSync(original_image_path)) {
                const imageBuffer = fs.readFileSync(original_image_path);
                await this.sock!.sendMessage(this.hubGroupJid!, {
                    image: imageBuffer,
                    caption: caption
                });
                logger.info(`Sent finding for ${identity_id} to hub group`);
                this.lastSendTime = Date.now();
            } else {
                logger.warn(`Original image path not found: ${original_image_path}. Sending text-only alert.`);
                await this.sock!.sendMessage(this.hubGroupJid!, {
                    text: `[Image Not Found] ${caption}`
                });
                this.lastSendTime = Date.now();
            }
        } catch (error) {
            logger.error({ err: error, group: this.hubGroupJid }, 'Failed to send message to findings hub');
            // If the group was deleted or bot was removed, this will throw.
            // We could set this.hubGroupJid = null here to pause until restart.
        }
    }
}

export const findingsHubSender = new FindingsHubSender();
