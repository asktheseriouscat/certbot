"""Common code for DNS Authenticator Plugins."""

import abc
import configobj
import logging
import os
import stat

from time import sleep

import zope.interface

from acme import challenges

from certbot import errors
from certbot import interfaces

from certbot.display import ops
from certbot.display import util as display_util

from certbot.plugins import common

logger = logging.getLogger(__name__)


@zope.interface.implementer(interfaces.IAuthenticator)
@zope.interface.provider(interfaces.IPluginFactory)
class DNSAuthenticator(common.Plugin):
    """Base class for DNS  Authenticators"""

    _attempt_cleanup = False

    @classmethod
    def add_parser_arguments(cls, add):
        add('propagation-seconds',
            default=10,
            type=int,
            help='The number of seconds to wait for DNS to propagate before asking the ACME server '
                 'to verify the DNS record.')

    def get_chall_pref(self, unused_domain): # pylint: disable=missing-docstring,no-self-use
        return [challenges.DNS01]

    def prepare(self): # pylint: disable=missing-docstring
        pass

    def perform(self, achalls): # pylint: disable=missing-docstring
        self._setup_credentials()

        self._attempt_cleanup = True

        responses = []
        for achall in achalls:
            domain = achall.domain
            validation_domain_name = achall.validation_domain_name(domain)
            validation = achall.validation(achall.account_key)

            self._perform(domain, validation_domain_name, validation)
            responses.append(achall.response(achall.account_key))

        # DNS updates take time to propagate and checking to see if the update has occurred is not
        # reliable (the machine this code is running on might be able to see an update before
        # the ACME server). So: we sleep for a short amount of time we believe to be long enough.
        sleep(self.conf('propagation-seconds'))

        return responses

    def cleanup(self, achalls):  # pylint: disable=missing-docstring
        if self._attempt_cleanup:
            for achall in achalls:
                domain = achall.domain
                validation_domain_name = achall.validation_domain_name(domain)
                validation = achall.validation(achall.account_key)

                self._cleanup(domain, validation_domain_name, validation)

    @abc.abstractmethod
    def _setup_credentials(self):  # pragma: no cover
        """
        Establish credentials, prompting if necessary.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _perform(self, domain, validation_domain_name, validation):  # pragma: no cover
        """
        Performs a dns-01 challenge by creating a DNS TXT record.

        :param string domain: The domain being validated.
        :param string validation_domain_name: The validation record domain name.
        :param string validation: The validation record content.
        :raises errors.PluginError: If the challenge cannot be performed
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def _cleanup(self, domain, validation_domain_name, validation):  # pragma: no cover
        """
        Deletes the DNS TXT record which would have been created by `_perform_achall`.

        Fails gracefully if no such record exists.

        :param string domain: The domain being validated.
        :param string validation_domain_name: The validation record domain name.
        :param string validation: The validation record content.
        """
        raise NotImplementedError()

    def _configure(self, key, label):
        """
        Ensure that a configuration value is available.

        If necessary, prompts the user and stores the result.

        :param string key: The configuration key.
        :param string label: The user-friendly label for this piece of information.
        """

        configured_value = self.conf(key)
        if not configured_value:
            new_value = self._prompt_for_data(label)

            setattr(self.config, self.dest(key), new_value)

    def _configure_file(self, key, label):
        """
        Ensure that a configuration value is available for a path.

        If necessary, prompts the user and stores the result.

        :param string key: The configuration key.
        :param string label: The user-friendly label for this piece of information.
        """

        configured_value = self.conf(key)
        if not configured_value:
            new_value = self._prompt_for_file(label)

            setattr(self.config, self.dest(key), os.path.abspath(os.path.expanduser(new_value)))

    def _configure_credentials(self, key, label, required_variables=None):
        """
        As `_configure_file`, but for a credential configuration file.

        If necessary, prompts the user and stores the result.

        Always stores absolute paths to avoid issues during renewal.

        :param string key: The configuration key.
        :param string label: The user-friendly label for this piece of information.
        :param dict required_variables: Map of variable which must be present to error to display.
        """

        self._configure_file(key, label)

        credentials_configuration = CredentialsConfiguration(self.conf(key), self.dest)
        if required_variables:
            credentials_configuration.require(required_variables)

        return credentials_configuration

    @staticmethod
    def _prompt_for_data(label):
        """
        Prompt the user for a piece of information.

        :param string label: The user-friendly label for this piece of information.
        :returns: The user's response (guaranteed non-empty).
        :rtype: string
        """

        def __validator(i):
            if not i:
                raise errors.PluginError('Please enter your {0}.'.format(label))

        code, response = ops.validated_input(
            __validator,
            'Input your {0}'.format(label),
            force_interactive=True)

        if code == display_util.OK:
            return response
        else:
            raise errors.PluginError('{0} required to proceed.'.format(label))

    @staticmethod
    def _prompt_for_file(label):
        """
        Prompt the user for a path.

        :param string label: The user-friendly label for the file.
        :returns: The user's response (guaranteed to exist).
        :rtype: string
        """

        def __validator(filename):
            if not filename:
                raise errors.PluginError('Please enter a valid path to your {0}.'.format(label))

            validate_file(os.path.expanduser(filename))

        code, response = ops.validated_directory(
            __validator,
            'Input the path to your {0}'.format(label),
            force_interactive=True)

        if code == display_util.OK:
            return response
        else:
            raise errors.PluginError('{0} required to proceed.'.format(label))


class CredentialsConfiguration(object):
    """Represents a user-supplied filed which stores API credentials."""

    def __init__(self, filename, mapper=lambda x: x):
        """
        :param string filename: A path to the configuration file.
        :param callable mapper: A transformation to apply to configuration key names
        :raises PluginError: If the file does not exist.
        """
        validate_file_permissions(filename)

        self.confobj = configobj.ConfigObj(filename)
        self.mapper = mapper

    def require(self, required_variables):
        """Ensures that the supplied set of variables are all present in the file.

        :param dict required_variables: Map of variable which must be present to error to display.
        :raises PluginError: If one or more are missing.
        """
        messages = []

        for var in required_variables:
            if not self._has(var):
                messages.append('Property "{0}" not found (should be {1}).'
                                .format(self.mapper(var), required_variables[var]))
            elif not self._get(var):
                messages.append('Property "{0}" not set (should be {1}).'
                                .format(self.mapper(var), required_variables[var]))

        if messages:
            raise errors.PluginError(
                'Missing {0} in credentials configuration file {1}:\n * {2}'.format(
                        'property' if len(messages) == 1 else 'properties',
                        self.confobj.filename,
                        '\n * '.join(messages)
                    )
            )

    def conf(self, var):
        """Find a configuration value for variable `var`, as transformed by `mapper`.

        :param string var: The variable to get.
        :returns: The value of the variable.
        :rtype: string
        """

        return self._get(var)

    def _has(self, var):
        return self.mapper(var) in self.confobj

    def _get(self, var):
        return self.confobj.get(self.mapper(var))


def validate_file(filename):
    """Ensure that the specified file exists."""

    if not os.path.exists(filename):
        raise errors.PluginError('File not found: {0}'.format(filename))

    if not os.path.isfile(filename):
        raise errors.PluginError('Path is not a file: {0}'.format(filename))


def validate_file_permissions(filename):
    """Ensure that the specified file exists and warn about unsafe permissions."""

    validate_file(filename)

    permissions = stat.S_IMODE(os.lstat(filename).st_mode)
    if permissions & stat.S_IRWXO:
        logger.warning('Unsafe permissions on credentials configuration file: %s', filename)


def base_domain_name_guesses(domain):
    """Return a list of progressively less-specific domain names.

    One of these will probably be the domain name known to the DNS provider.

    :Example:

    >>> base_domain_name_guesses('foo.bar.baz.example.com')
    ['foo.bar.baz.example.com', 'bar.baz.example.com', 'baz.example.com', 'example.com', 'com']

    :param string domain: The domain for which to return guesses.
    :returns: The a list of less specific domain names.
    :rtype: list
    """

    fragments = domain.split('.')
    return ['.'.join(fragments[i:]) for i in range(0, len(fragments))]
