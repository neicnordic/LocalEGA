"""Ensures communication with RabbitMQ Message Broker."""

import sys
import logging
import json
import ssl
from contextlib import contextmanager

import pika

from ..conf import CONF
from .logging import _cid

LOG = logging.getLogger(__name__)


class AMQPConnection():
    """
    Establishment of AMQP connections.
    """

    conn = None
    chann = None
    connection_params = None

    def __init__(self, conf_section='broker', on_failure=None):
        """
        Initialize AMQP class.

        :param conf_section: Section in the configuration file, defaults to 'broker'
        :type conf_section: str, optional
        :param on_failure: A callable object to be called in case of failure, defaults to None
        :type on_failure: callable object, optional
        """
        self.on_failure = on_failure
        self.conf_section = conf_section or 'broker'
        print('Conf section', self.conf_section)
        print('CONF', CONF)
        # assert self.conf_section in CONF.sections(), "Section not found in config file"

    def fetch_args(self):
        """
        Retrieve AMQP connection parameters.
        """
        LOG.info('Getting a connection to %s', self.conf_section)

        use_ssl = CONF.get_value(self.conf_section, 'ssl', default="no", conv=bool)
        LOG.debug("SSL Value from conf system: %s", type(use_ssl))

        # First try to get the config with URLParameters
        params = CONF.get_value(self.conf_section, 'connection', raw=True)
        if params:
            LOG.debug("Initializing a connection to: %s", params)
            self.connection_params = pika.connection.URLParameters(params)
            if params.startswith('amqps'):
                LOG.debug("Params have amqps, enabling ssl")
                use_ssl = True
        else:
            self.connection_params = pika.connection.ConnectionParameters(
                host=CONF.get_value(self.conf_section, 'hostname'),
                port=CONF.get_value(self.conf_section, 'port'),
                virtual_host=CONF.get_value(self.conf_section, 'virtual_host'),
                connection_attempts=CONF.get_value(self.conf_section, 'connection_attempts', default=30),
                heartbeat=CONF.get_value(self.conf_section, 'heartbeat', default=0),
                retry_delay=CONF.get_value(self.conf_section, 'retry_delay', default=10),
                credentials=pika.credentials.PlainCredentials(
                    username=CONF.get_value(self.conf_section, 'username'),
                    password=CONF.get_value(self.conf_section, 'password')
                )
            )

        # Handling the SSL options
        # Note: We re-create the SSL context, so don't pass any ssl_options in the above connection URI.
        LOG.debug("SSL before if: %s", use_ssl)
        if use_ssl:
            LOG.debug("SSL after if: %s", use_ssl)

            LOG.debug("Enforcing a TLS context")
            context = ssl.SSLContext(protocol=ssl.PROTOCOL_TLS)  # Enforcing (highest) TLS version (so... 1.2?)

            context.verify_mode = ssl.CERT_NONE
            # Require server verification
            if CONF.get_value(self.conf_section, 'verify_peer', conv=bool, default=False):
                LOG.debug("Require server verification")
                context.verify_mode = ssl.CERT_REQUIRED
                cacertfile = CONF.get_value(self.conf_section, 'cacertfile', default=None)
                if cacertfile:
                    context.load_verify_locations(cafile=cacertfile)

            # Check the server's hostname
            hostname = CONF.get_value(self.conf_section, 'hostname', default=None)
            verify_hostname = CONF.get_value(self.conf_section, 'verify_hostname', conv=bool, default=False)
            if verify_hostname:
                LOG.debug("Require hostname verification")
                assert hostname, "hostname must be set if verify_hostname is"
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED

            # If client verification is required
            certfile = CONF.get_value(self.conf_section, 'certfile', default=None)
            if certfile:
                LOG.debug("Prepare for client verification")
                keyfile = CONF.get_value(self.conf_section, 'keyfile')
                context.load_cert_chain(certfile, keyfile=keyfile)

            # Finally, the pika ssl options
            self.connection_params.ssl_options = pika.SSLOptions(context=context, server_hostname=hostname)

    def connect(self, force=False):
        """
        Make a blocking/select connection to the Message Broker supporting AMQP(S).

        :param force: Whether to force a new connection or not, defaults to False
        :type force: bool, optional
        """
        if force:
            self.close()

        if self.conn and self.chann:
            return

        if not self.connection_params:
            self.fetch_args()

        try:
            self.conn = pika.BlockingConnection(self.connection_params)  # this uses connection_attempts and retry_delay already
            self.chann = self.conn.channel()
            LOG.debug("Connection successful")
            return
        except (pika.exceptions.AMQPConnectionError,  # connection
                pika.exceptions.AMQPChannelError,     # channel
                pika.exceptions.ChannelError) as e:   # why did they keep that one separate?
            # See https://github.com/pika/pika/blob/master/pika/exceptions.py
            LOG.debug("MQ connection error: %r", e)
        except Exception as e:
            LOG.debug("Invalid connection: %r", e)

        # fail to connect
        if self.on_failure and callable(self.on_failure):
            LOG.error("Unable to connection to MQ")
            self.on_failure()

    @contextmanager
    def channel(self):
        """
        Retrieve connection channel.

        :yield: A blocking connection channel
        :rtype: pika.adapters.blocking_connection.BlockingChannel
        """
        if self.conn is None:
            self.connect()
        yield self.chann

    def close(self):
        """
        Close MQ channel.
        """
        LOG.debug("Closing the AMQP connection.")
        if self.chann and not self.chann.is_closed:
            self.chann.close()
        self.chann = None
        if self.conn and not self.conn.is_closed:
            self.conn.close()
        self.conn = None


# Instantiate a global instance
connection = AMQPConnection(on_failure=lambda: sys.exit(1))


def publish(message, exchange, routing, correlation_id=None):
    """
    Send a message to the local broker exchange using the given routing key.

    :param message: A dictionary representing the message to be published
    :type message: dict
    :param exchange: Exchange to publish to
    :type exchange: str
    :param routing: Routing key for the messages
    :type routing: str
    :param correlation_id: Id used to correlate responses with requests, defaults to None
    :type correlation_id: str, optional
    """
    correlation_id = correlation_id or _cid.get()
    assert(correlation_id), "You should not publish without a correlation id"
    with connection.channel() as channel:
        LOG.debug('Sending to exchange: %s [routing key: %s]', exchange, routing, extra={'correlation_id': correlation_id})
        channel.basic_publish(exchange,             # exchange
                              routing,              # routing_key
                              json.dumps(message),  # body
                              properties=pika.BasicProperties(correlation_id=correlation_id,
                                                              content_type='application/json',
                                                              delivery_mode=2))


def consume(work, from_queue, to_routing, ack_on_error=True):
    """
    Register callback ``work`` to be called, blocking function.

    If there are no message in ``from_queue``, the function blocks and waits for new messages.

    If the function ``work`` returns a non-None message, the latter is published
    to the `lega` exchange with ``to_routing`` as the routing key

    :param work: A callable object to register as callback
    :type work: callable object
    :param from_queue: Queue to consume messages from
    :type from_queue: str
    :param to_routing: Routing key for the local exchange messages
    :type to_routing: str
    :param ack_on_error: Send ACK in case of error, defaults to True
    :type ack_on_error: bool, optional
    """
    LOG.debug('Consuming message from %s', from_queue)
    assert(from_queue)

    def process_request(channel, method_frame, props, body):
        """
        Process an AMQP request.

        :param channel: Blocking channel used to send the ACK
        :type channel: pika.adapters.blocking_connection.BlockingChannel
        :param method_frame: Carries with it the request or response that's being sent to or received
        :type method_frame: pika.Frame.Method
        :param props: Properties of remote procedure call used to get the correlation id
        :type props: pika.spec.BasicProperties
        :param body: Body of the consumed message
        :type body: str
        :return: To end the execution of the function call
        """
        correlation_id = props.correlation_id
        message_id = method_frame.delivery_tag
        try:
            _cid.set(correlation_id)
            LOG.debug('Consuming message %s', message_id, extra={'correlation_id': correlation_id})

            # Process message in JSON format
            try:
                content = json.loads(body)
            except Exception as e:
                LOG.error('Malformed JSON-message: %s', e, extra={'correlation_id': correlation_id})
                LOG.error('Original message: %s', body, extra={'correlation_id': correlation_id})
                err_msg = {
                    'reason': 'Malformed JSON-message',
                    'original_message': body.decode(errors='ignore')  # or str(body) ?
                }
                publish(err_msg, 'cega', 'files.error', correlation_id=correlation_id)
                # Force acknowledging the message
                channel.basic_ack(delivery_tag=message_id)
                return  # game over

            # Message correctly formed
            answer, error = work(content)  # exceptions already caught by decorator

            # Publish the answer
            if answer:
                assert(to_routing)
                publish(answer, 'lega', to_routing, correlation_id=correlation_id)

            # Acknowledgment: Cancel the message resend in case MQ crashes
            if not error or ack_on_error:
                LOG.debug('Sending ACK for message: %s', message_id)
                channel.basic_ack(delivery_tag=message_id)
        finally:
            _cid.set(None)

    # Let's do this
    LOG.debug('MQ setup')
    while True:
        with connection.channel() as channel:
            try:
                LOG.debug('Consuming message from %s', from_queue)
                channel.basic_qos(prefetch_count=1)  # One job per worker
                channel.basic_consume(from_queue, on_message_callback=process_request)
                channel.start_consuming()
            except KeyboardInterrupt:
                channel.stop_consuming()
                connection.close()
                break
            except (pika.exceptions.AMQPConnectionError,
                    pika.exceptions.AMQPChannelError,
                    pika.exceptions.ChannelError,
                    pika.exceptions.AMQPError) as e:
                LOG.debug('Retrying after %s', e)
                connection.close()
                continue
            # # Note: Let it raise any other exception and bail out.
            # except Exception as e:
            #     LOG.critical('%r', e)
            #     connection.close()
            #     break
            #     #sys.exit(2)
