import json
import os
import re
from urllib.parse import urljoin

from ..console_write import console_write
from ..download_manager import downloader, update_url
from ..versions import version_sort
from .provider_exception import ProviderException
from .schema_compat import platforms_to_releases
from .schema_compat import SchemaVersion


class InvalidChannelFileException(ProviderException):

    def __init__(self, channel, reason_message):
        super().__init__(
            'Channel %s does not appear to be a valid channel file because'
            ' %s' % (channel.url, reason_message))


class ChannelProvider:
    """
    Retrieves a channel and provides an API into the information

    The current channel/repository infrastructure caches repository info into
    the channel to improve the Package Control client performance. This also
    has the side effect of lessening the load on the GitHub and BitBucket APIs
    and getting around not-infrequent HTTP 503 errors from those APIs.

    :param channel_url:
        The URL of the channel

    :param settings:
        A dict containing at least the following fields:
          `cache_length`,
          `debug`,
          `timeout`,
          `user_agent`
        Optional fields:
          `http_proxy`,
          `https_proxy`,
          `proxy_username`,
          `proxy_password`,
          `query_string_params`
    """

    __slots__ = [
        'channel_info',
        'channel_url',
        'schema_version',
        'settings',
    ]

    def __init__(self, channel_url, settings):
        self.channel_info = None
        self.channel_url = channel_url
        self.schema_version = None
        self.settings = settings

    @classmethod
    def match_url(cls, channel_url):
        """
        Indicates if this provider can handle the provided channel_url.
        """

        return True

    def prefetch(self):
        """
        Go out and perform HTTP operations, caching the result

        :raises:
            ProviderException: when an error occurs trying to open a file
            DownloaderException: when an error occurs trying to open a URL
        """

        self.fetch()

    def fetch(self):
        """
        Retrieves and loads the JSON for other methods to use

        :raises:
            InvalidChannelFileException: when parsing or validation file content fails
            ProviderException: when an error occurs trying to open a file
            DownloaderException: when an error occurs trying to open a URL
        """

        if self.channel_info is not None:
            return

        if re.match(r'https?://', self.channel_url, re.I):
            with downloader(self.channel_url, self.settings) as manager:
                json_string = manager.fetch(self.channel_url, 'Error downloading channel.')

        # All other channels are expected to be filesystem paths
        else:
            if not os.path.exists(self.channel_url):
                raise ProviderException('Error, file %s does not exist' % self.channel_url)

            if self.settings.get('debug'):
                console_write(
                    '''
                    Loading %s as a channel
                    ''',
                    self.channel_url
                )

            # We open as binary so we get bytes like the DownloadManager
            with open(self.channel_url, 'rb') as f:
                json_string = f.read()

        try:
            channel_info = json.loads(json_string.decode('utf-8'))
        except (ValueError):
            raise InvalidChannelFileException(self, 'parsing JSON failed.')

        try:
            schema_version = SchemaVersion(channel_info['schema_version'])
        except KeyError:
            raise InvalidChannelFileException(self, 'the "schema_version" JSON key is missing.')
        except ValueError as e:
            raise InvalidChannelFileException(self, e)

        # Fix any out-dated repository URLs in the package cache
        debug = self.settings.get('debug')
        packages_key = 'packages_cache' if schema_version.major >= 2 else 'packages'
        if packages_key in channel_info:
            original_cache = channel_info[packages_key]
            new_cache = {}
            for repo in original_cache:
                new_cache[update_url(repo, debug)] = original_cache[repo]
            channel_info[packages_key] = new_cache

        self.channel_info = channel_info
        self.schema_version = schema_version

    def get_name_map(self):
        """
        :raises:
            ProviderException: when an error occurs with the channel contents
            DownloaderException: when an error occurs trying to open a URL

        :return:
            A dict of the mapping for URL slug -> package name
        """

        self.fetch()

        if self.schema_version.major >= 2:
            return {}

        return self.channel_info.get('package_name_map', {})

    def get_renamed_packages(self):
        """
        :raises:
            ProviderException: when an error occurs with the channel contents
            DownloaderException: when an error occurs trying to open a URL

        :return:
            A dict of the packages that have been renamed
        """

        self.fetch()

        if self.schema_version.major >= 2:
            output = {}
            if 'packages_cache' in self.channel_info:
                for repo in self.channel_info['packages_cache']:
                    for package in self.channel_info['packages_cache'][repo]:
                        previous_names = package.get('previous_names', [])
                        if not isinstance(previous_names, list):
                            previous_names = [previous_names]
                        for previous_name in previous_names:
                            output[previous_name] = package['name']
            return output

        return self.channel_info.get('renamed_packages', {})

    def get_repositories(self):
        """
        :raises:
            ProviderException: when an error occurs with the channel contents
            DownloaderException: when an error occurs trying to open a URL

        :return:
            A list of the repository URLs
        """

        self.fetch()

        if 'repositories' not in self.channel_info:
            raise InvalidChannelFileException(
                self, 'the "repositories" JSON key is missing.')

        # Determine a relative root so repositories can be defined
        # relative to the location of the channel file.
        scheme_match = re.match(r'(https?:)//', self.channel_url, re.I)
        if scheme_match is None:
            relative_base = os.path.dirname(self.channel_url)
            is_http = False
        else:
            is_http = True

        debug = self.settings.get('debug')
        output = []
        for repository in self.channel_info['repositories']:
            if repository.startswith('//'):
                if scheme_match is not None:
                    repository = scheme_match.group(1) + repository
                else:
                    repository = 'https:' + repository
            elif repository.startswith('/'):
                # We don't allow absolute repositories
                continue
            elif repository.startswith('./') or repository.startswith('../'):
                if is_http:
                    repository = urljoin(self.channel_url, repository)
                else:
                    repository = os.path.join(relative_base, repository)
                    repository = os.path.normpath(repository)
            output.append(update_url(repository, debug))

        return output

    def get_sources(self):
        """
        Return a list of current URLs that are directly referenced by the
        channel

        :return:
            A list of URLs and/or file paths
        """

        return self.get_repositories()

    def get_packages(self, repo_url):
        """
        Provides access to the repository info that is cached in a channel

        :param repo_url:
            The URL of the repository to get the cached info of

        :raises:
            ProviderException: when an error occurs with the channel contents
            DownloaderException: when an error occurs trying to open a URL

        :return:
            A dict in the format:
            {
                'Package Name': {
                    'name': name,
                    'description': description,
                    'author': author,
                    'homepage': homepage,
                    'last_modified': last modified date,
                    'releases': [
                        {
                            'sublime_text': '*',
                            'platforms': ['*'],
                            'url': url,
                            'date': date,
                            'version': version
                        }, ...
                    ],
                    'previous_names': [old_name, ...],
                    'labels': [label, ...],
                    'readme': url,
                    'issues': url,
                    'donate': url,
                    'buy': url
                },
                ...
            }
        """

        self.fetch()

        repo_url = update_url(repo_url, self.settings.get('debug'))

        # The 2.0 channel schema renamed the key cached package info was
        # stored under in order to be more clear to new users.
        packages_key = 'packages_cache' if self.schema_version.major >= 2 else 'packages'

        output = {}
        for package in self.channel_info.get(packages_key, {}).get(repo_url, []):
            copy = package.copy()

            # In schema version 2.0, we store a list of dicts containing info
            # about all available releases. These include "version" and
            # "platforms" keys that are used to pick the download for the
            # current machine.
            if self.schema_version.major < 2:
                copy['releases'] = platforms_to_releases(copy, self.settings.get('debug'))
                del copy['platforms']
            else:
                last_modified = None

                for release in copy.get('releases', []):
                    date = release.get('date')
                    if not last_modified or (date and date > last_modified):
                        last_modified = date

                    if self.schema_version.major < 4:
                        if 'dependencies' in release:
                            release['libraries'] = release['dependencies']
                            del release['dependencies']

                copy['last_modified'] = last_modified

            defaults = {
                'buy': None,
                'issues': None,
                'labels': [],
                'previous_names': [],
                'readme': None,
                'donate': None
            }
            for field in defaults:
                if field not in copy:
                    copy[field] = defaults[field]

            copy['releases'] = version_sort(copy['releases'], 'platforms', reverse=True)

            output[copy['name']] = copy

        return output

    def get_libraries(self, repo_url):
        """
        Provides access to the library info that is cached in a channel

        :param repo_url:
            The URL of the repository to get the cached info of

        :raises:
            ProviderException: when an error occurs with the channel contents
            DownloaderException: when an error occurs trying to open a URL

        :return:
            A dict in the format:
            {
                'Library Name': {
                    'name': name,
                    'load_order': two digit string,
                    'description': description,
                    'author': author,
                    'issues': URL,
                    'releases': [
                        {
                            'sublime_text': '*',
                            'platforms': ['*'],
                            'url': url,
                            'date': date,
                            'version': version,
                            'sha256': hex_hash
                        }, ...
                    ]
                },
                ...
            }
        """

        self.fetch()

        repo_url = update_url(repo_url, self.settings.get('debug'))

        # The 4.0.0 channel schema renamed the key cached package info was
        # stored under in order to be more clear to new users.
        libraries_key = 'libraries_cache' if self.schema_version.major >= 4 else 'dependencies_cache'

        output = {}
        for library in self.channel_info.get(libraries_key, {}).get(repo_url, []):
            library['releases'] = version_sort(library['releases'], 'platforms', reverse=True)
            output[library['name']] = library

        return output
