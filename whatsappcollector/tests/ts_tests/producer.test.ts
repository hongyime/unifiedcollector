import { BrokerProducer } from '../../services/wa-client-ts/src/producer';
import amqplib from 'amqplib';
import Redis from 'ioredis';

jest.mock('amqplib');
jest.mock('ioredis');

describe('Broker Producer', () => {
    let producer: BrokerProducer;

    beforeEach(() => {
        jest.clearAllMocks();
        process.env.BROKER_TYPE = 'rabbitmq';
        process.env.RABBITMQ_URL = 'amqp://user:pass@rabbitmq:5672/';
        process.env.REDIS_URL = 'redis://:password@redis:6379/0';
        producer = new BrokerProducer();
    });

    it('should connect to rabbitmq successfully', async () => {
        const mockChannel = {
            assertExchange: jest.fn().mockResolvedValue(true)
        };
        const mockConn = {
            createConfirmChannel: jest.fn().mockResolvedValue(mockChannel),
            on: jest.fn()
        };
        (amqplib.connect as jest.Mock).mockResolvedValue(mockConn);

        await producer.connect(1, 1);

        expect(amqplib.connect).toHaveBeenCalled();
        expect(mockConn.createConfirmChannel).toHaveBeenCalled();
        expect(mockChannel.assertExchange).toHaveBeenCalledWith('whatsapp.events', 'topic', { durable: true });
    });

    it('should connect to redis successfully if BROKER_TYPE is redis', async () => {
        process.env.BROKER_TYPE = 'redis';
        producer = new BrokerProducer();

        const mockRedisOn = jest.fn();
        const mockRedisConnect = jest.fn().mockResolvedValue(undefined);
        (Redis as unknown as jest.Mock).mockImplementation(() => ({
            on: mockRedisOn,
            connect: mockRedisConnect
        }));

        await producer.connect(1, 1);
        expect(Redis).toHaveBeenCalled();
        expect(mockRedisOn).toHaveBeenCalledWith('error', expect.any(Function));
        expect(mockRedisConnect).toHaveBeenCalled();
    });

    it('should publish to rabbitmq correctly', async () => {
        const mockPublish = jest.fn((exchange, routingKey, payload, options, callback) => {
            callback(null); // Success
        });
        const mockChannel = {
            assertExchange: jest.fn().mockResolvedValue(true),
            publish: mockPublish
        };
        const mockConn = {
            createConfirmChannel: jest.fn().mockResolvedValue(mockChannel),
            on: jest.fn()
        };
        (amqplib.connect as jest.Mock).mockResolvedValue(mockConn);

        await producer.connect(1, 1);

        await producer.publish('test.event', { message_id: '123' });

        expect(mockPublish).toHaveBeenCalledWith(
            'whatsapp.events',
            'test.event',
            expect.any(Buffer),
            { persistent: true },
            expect.any(Function)
        );
    });

    it('should preserve correlation metadata and route messages.history', async () => {
        const previousSessionName = process.env.SESSION_NAME;
        process.env.SESSION_NAME = 'default';

        const publishedPayloads: Buffer[] = [];
        const mockPublish = jest.fn((exchange, routingKey, payload, options, callback) => {
            publishedPayloads.push(payload as Buffer);
            callback(null);
        });
        const mockChannel = {
            assertExchange: jest.fn().mockResolvedValue(true),
            publish: mockPublish
        };
        const mockConn = {
            createConfirmChannel: jest.fn().mockResolvedValue(mockChannel),
            on: jest.fn()
        };
        (amqplib.connect as jest.Mock).mockResolvedValue(mockConn);

        await producer.connect(1, 1);

        await producer.publish('messages.history', {
            sync_type: 'ON_DEMAND',
            correlation_id: 'corr-1',
            messages: []
        });

        expect(mockPublish).toHaveBeenCalledWith(
            'whatsapp.events',
            'messages.history',
            expect.any(Buffer),
            { persistent: true },
            expect.any(Function)
        );

        const published = JSON.parse(publishedPayloads[0].toString('utf8'));
        expect(published._metadata.correlation_id).toBe('corr-1');
        expect(published._metadata.session_name).toBe('default');

        process.env.SESSION_NAME = previousSessionName;
    });

    it('should retry rabbitmq connection with backoff and eventually succeed', async () => {
        const mockChannel = {
            assertExchange: jest.fn().mockResolvedValue(true)
        };
        const mockConn = {
            createConfirmChannel: jest.fn().mockResolvedValue(mockChannel),
            on: jest.fn()
        };

        (amqplib.connect as jest.Mock)
            .mockRejectedValueOnce(new Error('boot race'))
            .mockRejectedValueOnce(new Error('still starting'))
            .mockResolvedValue(mockConn);

        await producer.connect(1, 3);

        expect(amqplib.connect).toHaveBeenCalledTimes(3);
        expect(mockConn.createConfirmChannel).toHaveBeenCalledTimes(1);
        expect(mockChannel.assertExchange).toHaveBeenCalledWith('whatsapp.events', 'topic', { durable: true });
    });
});
