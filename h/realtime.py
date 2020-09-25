import base64
import random
import struct
from logging import getLogger

import kombu
from kombu.exceptions import LimitExceeded, OperationalError
from kombu.mixins import ConsumerMixin
from kombu.pools import producers as producer_pool

from h.exceptions import RealtimeMessageQueueError

LOG = getLogger(__name__)


class Consumer(ConsumerMixin):
    """
    A realtime consumer that listens to the configured routing key and calls
    the wrapped handler function on receiving a matching message.

    Conforms to the :py:class:`kombu.mixins.ConsumerMixin` interface.

    :param connection: a `kombe.Connection`
    :param routing_key: listen to messages with this routing key
    :param handler: the function which gets called when a messages arrives
    """

    def __init__(self, connection, routing_key, handler):
        self.connection = connection
        self.routing_key = routing_key
        self.handler = handler
        self.exchange = get_exchange()

    def get_consumers(self, consumer_factory, channel):
        name = self.generate_queue_name()
        queue = kombu.Queue(
            name,
            self.exchange,
            durable=False,
            routing_key=self.routing_key,
            auto_delete=True,
        )
        return [consumer_factory(queues=[queue], callbacks=[self.handle_message])]

    def generate_queue_name(self):
        return "realtime-{}-{}".format(self.routing_key, self._random_id())

    def handle_message(self, body, message):
        """
        Handles a realtime message by acknowledging it and then calling the
        wrapped handler.
        """
        message.ack()
        self.handler(body)

    def _random_id(self):
        """Generate a short random string"""
        data = struct.pack("Q", random.getrandbits(64))
        return base64.urlsafe_b64encode(data).strip(b"=")


class Publisher:
    """
    A realtime publisher for publishing messages to all subscribers.

    An instance of this publisher is available on Pyramid requests
    with `request.realtime`.

    :param request: a `pyramid.request.Request`
    """

    def __init__(self, request):
        self.connection = get_connection(request.registry.settings, fail_fast=True)
        self.exchange = get_exchange()

    def publish_annotation(self, payload):
        """Publish an annotation message with the routing key 'annotation'.

        :raise RealtimeMessageQueueError: When we cannot queue the message
        """
        self._publish("annotation", payload)

    def publish_user(self, payload):
        """Publish a user message with the routing key 'user'.

        :raise RealtimeMessageQueueError: When we cannot queue the message
        """
        self._publish("user", payload)

    def _publish(self, routing_key, payload):
        retry_policy = {"max_retries": 5, "interval_start": 0.2, "interval_step": 0.3}

        try:
            with producer_pool[self.connection].acquire(
                block=True, timeout=1
            ) as producer:
                producer.publish(
                    payload,
                    exchange=self.exchange,
                    declare=[self.exchange],
                    routing_key=routing_key,
                    retry=True,
                    retry_policy=retry_policy,
                )

        except (OperationalError, LimitExceeded) as err:
            # If we fail to connect (OperationalError), or we don't get a
            # producer from the pool in time (LimitExceeded) raise
            LOG.error("Failed to queue realtime message with error %s", err)
            LOG.debug("Failed message payload was: %s", payload)
            raise RealtimeMessageQueueError() from err


def get_exchange():
    """Returns a configures `kombu.Exchange` to use for realtime messages."""

    return kombu.Exchange(
        "realtime", type="direct", durable=False, delivery_mode="transient"
    )


def get_connection(settings, fail_fast=False):
    """Returns a `kombu.Connection` based on the application's settings.

    :param settings: Application settings
    :param fail_fast: Make the connection fail if we cannot get a connection
        quickly.
    """

    conn = settings.get("broker_url", "amqp://guest:guest@localhost:5672//")

    kwargs = {}

    if fail_fast:
        kwargs["transport_options"] = {
            # Connection fallback set by`kombu.connection._extract_failover_opts`
            # Which are used when retrying a connection as sort of documented here:
            # https://kombu.readthedocs.io/en/latest/reference/kombu.connection.html#kombu.connection.Connection.ensure_connection
            # Maximum number of times to retry. If this limit is exceeded the
            # connection error will be re-raised
            "max_retries": 2,
            # The number of seconds we start sleeping for (when retrying)
            "interval_start": 0.1,
            #  How many seconds added to the interval for each retry
            "interval_step": 0.1,
            # Maximum number of seconds to sleep between each retry
            "interval_max": 1.0,
        }

    return kombu.Connection(conn, **kwargs)


def includeme(config):
    config.add_request_method(Publisher, name="realtime", reify=True)
