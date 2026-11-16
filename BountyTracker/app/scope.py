"""Validate passive imports against explicit, program-owned scope rules."""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def canonical_host(value):
    value = value.rstrip('.').lower()
    if not value or '%' in value:
        raise ValueError('Invalid host.')
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        pass
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise ValueError('Invalid host.') from exc
    if len(value) > 253 or not all(
        re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')
    ):
        raise ValueError('Invalid host.')
    return value


def canonical_path(value):
    path = value or '/'
    if (not path.startswith('/') or '//' in path or '\\' in path or ';' in path or
            re.search(r'[\x00-\x20\x7f]|%(?![0-9a-fA-F]{2})|%(?:2e|2f|5c|25|3f|23|3b|00)', path, re.IGNORECASE) or
            any(part in ('.', '..') for part in path.split('/'))):
        raise ValueError('Invalid or ambiguous URL path.')
    def normalize_escape(match):
        character = chr(int(match[0][1:], 16))
        return character if re.fullmatch(r'[a-zA-Z0-9_~-]', character) else match[0].upper()
    return re.sub(r'%[0-9a-fA-F]{2}', normalize_escape, path)


def parse_request_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid request URL.')
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError('Expected an HTTP or HTTPS URL without credentials.')
    host = canonical_host(url.hostname)
    port = url.port
    path = canonical_path(url.path)
    authority = f'[{host}]' if ':' in host else host
    if port is not None and (url.scheme, port) not in (('http', 80), ('https', 443)):
        authority += f':{port}'
    return host, path, urlunsplit((url.scheme, authority, path, '', ''))


@dataclass(frozen=True)
class Rule:
    host: str
    wildcard: bool
    path: str | None
    denied: bool

    def matches(self, host, path):
        if self.wildcard:
            if not host.endswith('.' + self.host):
                return False
        elif host != self.host:
            return False
        return self.path is None or (path is not None and
                                     (path == self.path or path.startswith(self.path + '/')))


def parse_scope(text):
    rules = []
    for index, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        denied = line.startswith('!')
        value = line[1:] if denied else line
        wildcard = value.startswith('*.')
        value = value[2:] if wildcard else value
        host, separator, path = value.partition('/')
        try:
            if '?' in value or '#' in value:
                raise ValueError('Queries and fragments are unsupported.')
            host = canonical_host(host)
            if wildcard and ('.' not in host or ':' in host or host.replace('.', '').isdigit()):
                raise ValueError('Wildcards require a domain suffix.')
            path = canonical_path('/' + path).rstrip('/') if separator else None
        except ValueError as error:
            raise ValueError(f'Scope line {index}: use a host or host/path, e.g. example.com/api.') from error
        rules.append(Rule(host, wildcard, path or None, denied))
    return rules


def in_scope(rules, host, path=None):
    matching = [rule for rule in rules if rule.matches(host, path)]
    return bool(matching) and not any(rule.denied for rule in matching)


def asset_record(line):
    """Read a host from one plain-text or Subfinder/httpx JSONL record."""
    import json

    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        record = line
    status = ''
    candidates = (record,)
    if isinstance(record, dict):
        status = str(record.get('status_code') or '')
        candidates = (record.get('url'), record.get('host'), record.get('input'))
    for value in candidates:
        if not isinstance(value, str):
            continue
        value = value.strip()
        try:
            if '://' in value:
                host, _, _ = parse_request_url(value)
            elif re.fullmatch(r'(?:\[[^\]]+\]|[^:/@?#]+):[0-9]+', value):
                host, _, _ = parse_request_url(f'http://{value}')
            else:
                host = canonical_host(value)
        except ValueError:
            continue
        return host, status
    raise ValueError('Expected a hostname.')


def endpoint_location(value):
    """Read host, path and method from stored endpoint text without changing legacy rows."""
    value = value.strip()
    verb, separator, target = value.partition(' ')
    if separator and re.fullmatch(r"[A-Za-z][A-Za-z0-9!#$%&'*+.^_`|~-]{0,15}", verb):
        method, location = verb.upper(), target.strip()
    else:
        method, location = '', value
    if location.startswith(('https://', 'http://')):
        try:
            host, path, _ = parse_request_url(location)
            return host, path, method
        except ValueError:
            pass
    return '', location.partition('?')[0] if location.startswith('/') else '', method
