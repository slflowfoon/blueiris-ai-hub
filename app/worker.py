import os
import logging
import redis
from rq import Worker, Queue
from service_health import start_heartbeat_thread

listen = ['default']
redis_url = os.getenv('REDIS_URL', 'redis://redis:6379/0')

# Establish connection
conn = redis.from_url(redis_url)

for name in ["rq.worker", "rq.job", "rq.queue"]:
    logging.getLogger(name).setLevel(logging.WARNING)

if __name__ == '__main__':
    start_heartbeat_thread("worker")
    # Fix: Explicitly create Queues with the connection
    queues = [Queue(name, connection=conn) for name in listen]
    
    # Start the worker
    worker = Worker(queues, connection=conn)
    worker.work(logging_level="WARNING")
