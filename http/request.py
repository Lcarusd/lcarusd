from __future__ import unicode_literals

import copy
import os
import re
import sys
from io import BytesIO
from itertools import chain
from pprint import pformat

from django.conf import settings
from django.core import signing
from django.core.exceptions import DisallowedHost, ImproperlyConfigured
from django.core.files import uploadhandler
from django.http.multipartparser import MultiPartParser, MultiPartParserError
from django.utils import six
from django.utils.datastructures import ImmutableList, MultiValueDict
from django.utils.encoding import (
    escape_uri_path, force_bytes, force_str, force_text, iri_to_uri,
)
from django.utils.six.moves.urllib.parse import (
    parse_qsl, quote, urlencode, urljoin, urlsplit,
)

RAISE_ERROR = object()
host_validation_re = re.compile(
    r"^([a-z0-9.-]+|\[[a-f0-9]*:[a-f0-9:]+\])(:\d+)?$")


class UnreadablePostError(IOError):
    pass


class RawPostDataException(Exception):
    """
    You cannot access raw_post_data from a request that has
    multipart/* POST data if it has been accessed via POST,
    FILES, etc..
    """
    pass


class HttpRequest(object):
    """基本的HTTP请求。"""

    # GET/POST字典中使用的编码。 None意味着使用默认设置。
    _encoding = None
    _upload_handlers = []

    def __init__(self):


        # 警告：`WSGIRequest`子类不会调用`super`。
        # 这里所做的任何变量赋值都应该在`WSGIRequest .__init__()`中发生。

        self.GET = QueryDict(mutable=True)
        self.POST = QueryDict(mutable=True)
        self.COOKIES = {}
        self.META = {}
        self.FILES = MultiValueDict()

        self.path = ''
        self.path_info = ''
        self.method = None
        self.resolver_match = None
        self._post_parse_error = False

    def __repr__(self):
        if self.method is None or not self.get_full_path():
            return force_str('<%s>' % self.__class__.__name__)
        return force_str(
            '<%s: %s %r>' % (self.__class__.__name__, self.method, force_str(self.get_full_path()))
        )

    def get_host(self):
        """使用环境或请求标头返回HTTP主机。"""
        # 我们尝试三种选择，按照优先顺序递减的顺序。
        if settings.USE_X_FORWARDED_HOST and ('HTTP_X_FORWARDED_HOST' in self.META):
            host = self.META['HTTP_X_FORWARDED_HOST']
        elif 'HTTP_HOST' in self.META:
            host = self.META['HTTP_HOST']
        else:
            # 使用来自PEP 333的算法重建主机。
            host = self.META['SERVER_NAME']
            server_port = str(self.META['SERVER_PORT'])
            if server_port != ('443' if self.is_secure() else '80'):
                host = '%s:%s' % (host, server_port)

        # 如果ALLOWED_HOSTS为空并且DEBUG = True，则允许本地主机的变体。
        allowed_hosts = settings.ALLOWED_HOSTS
        if settings.DEBUG and not allowed_hosts:
            allowed_hosts = ['localhost', '127.0.0.1', '[::1]']

        domain, port = split_domain_port(host)
        if domain and validate_host(domain, allowed_hosts):
            return host
        else:
            msg = "Invalid HTTP_HOST header: %r." % host
            if domain:
                msg += " You may need to add %r to ALLOWED_HOSTS." % domain
            else:
                msg += " The domain name provided is not valid according to RFC 1034/1035."
            raise DisallowedHost(msg)

    def get_full_path(self):
        # RFC 3986要求查询字符串参数在ASCII范围内。
        # 如果没有发生，我们不会崩溃，我们会进行防御式编码。
        return '%s%s' % (
            escape_uri_path(self.path),
            ('?' + iri_to_uri(self.META.get('QUERY_STRING', ''))) if self.META.get('QUERY_STRING', '') else ''
        )

    def get_signed_cookie(self, key, default=RAISE_ERROR, salt='', max_age=None):
        """
        尝试返回已签名的cookie。 
        
        如果签名失败或Cookie已过期，则会引发异常...，
        除非您提供默认参数，否则将返回该值。
        """
        try:
            cookie_value = self.COOKIES[key]
        except KeyError:
            if default is not RAISE_ERROR:
                return default
            else:
                raise
        try:
            value = signing.get_cookie_signer(salt=key + salt).unsign(
                cookie_value, max_age=max_age)
        except signing.BadSignature:
            if default is not RAISE_ERROR:
                return default
            else:
                raise
        return value

    def build_absolute_uri(self, location=None):
        """
        根据此请求中可用的位置和变量生成绝对URI。 
        如果没有指定``location``，绝对URI就建立在``request.get_full_path（）``上。 
        无论如何，如果该位置是绝对位置的，它将被简单地转换为符合RFC 3987的URI并返回，
        并且如果位置是相对的或者是与方案相关的（即，“example.com /”），
        则它被链接到从请求变量构造的基本URL。
        """
        if location is None:
            # 将其设置为路径以'//'开头的边缘情况的绝对url（但无模式和无域）。
            location = '//%s' % self.get_full_path()
        bits = urlsplit(location)
        if not (bits.scheme and bits.netloc):
            current_uri = '{scheme}://{host}{path}'.format(scheme=self.scheme,
                                                           host=self.get_host(),
                                                           path=self.path)
            # 使用提供的位置加入构建的URL，这将允许提供的“位置”将查询字符串应用于基本路径，并覆盖主机，如果以//开头//
            location = urljoin(current_uri, location)
        return iri_to_uri(location)

    def _get_scheme(self):
        return 'https' if os.environ.get("HTTPS") == "on" else 'http'

    @property
    def scheme(self):
        # 首先，检查SECURE_PROXY_SSL_HEADER设置。
        if settings.SECURE_PROXY_SSL_HEADER:
            try:
                header, value = settings.SECURE_PROXY_SSL_HEADER
            except ValueError:
                raise ImproperlyConfigured(
                    'SECURE_PROXY_SSL_HEADER设置必须是包含两个值的元组。'
                )
            if self.META.get(header, None) == value:
                return 'https'
        # 否则，回退到_get_scheme（），这是子类实现的钩子。
        return self._get_scheme()

    def is_secure(self):
        return self.scheme == 'https'

    def is_ajax(self):
        return self.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'

    @property
    def encoding(self):
        return self._encoding

    @encoding.setter
    def encoding(self, val):
        """
        Sets the encoding used for GET/POST accesses. If the GET or POST
        dictionary has already been created, it is removed and recreated on the
        next access (so that it is decoded correctly).
        """
        self._encoding = val
        if hasattr(self, '_get'):
            del self._get
        if hasattr(self, '_post'):
            del self._post

    def _initialize_handlers(self):
        self._upload_handlers = [uploadhandler.load_handler(handler, self)
                                 for handler in settings.FILE_UPLOAD_HANDLERS]

    @property
    def upload_handlers(self):
        if not self._upload_handlers:
            # If there are no upload handlers defined, initialize them from settings.
            self._initialize_handlers()
        return self._upload_handlers

    @upload_handlers.setter
    def upload_handlers(self, upload_handlers):
        if hasattr(self, '_files'):
            raise AttributeError("You cannot set the upload handlers after the upload has been processed.")
        self._upload_handlers = upload_handlers

    def parse_file_upload(self, META, post_data):
        """Returns a tuple of (POST QueryDict, FILES MultiValueDict)."""
        self.upload_handlers = ImmutableList(
            self.upload_handlers,
            warning="You cannot alter upload handlers after the upload has been processed."
        )
        parser = MultiPartParser(META, post_data, self.upload_handlers, self.encoding)
        return parser.parse()

    @property
    def body(self):
        if not hasattr(self, '_body'):
            if self._read_started:
                raise RawPostDataException("You cannot access body after reading from request's data stream")
            try:
                self._body = self.read()
            except IOError as e:
                six.reraise(UnreadablePostError, UnreadablePostError(*e.args), sys.exc_info()[2])
            self._stream = BytesIO(self._body)
        return self._body

    def _mark_post_parse_error(self):
        self._post = QueryDict('')
        self._files = MultiValueDict()
        self._post_parse_error = True

    def _load_post_and_files(self):
        """Populate self._post and self._files if the content-type is a form type"""
        if self.method != 'POST':
            self._post, self._files = QueryDict('', encoding=self._encoding), MultiValueDict()
            return
        if self._read_started and not hasattr(self, '_body'):
            self._mark_post_parse_error()
            return

        if self.META.get('CONTENT_TYPE', '').startswith('multipart/form-data'):
            if hasattr(self, '_body'):
                # Use already read data
                data = BytesIO(self._body)
            else:
                data = self
            try:
                self._post, self._files = self.parse_file_upload(self.META, data)
            except MultiPartParserError:
                # An error occurred while parsing POST data. Since when
                # formatting the error the request handler might access
                # self.POST, set self._post and self._file to prevent
                # attempts to parse POST data again.
                # Mark that an error occurred. This allows self.__repr__ to
                # be explicit about it instead of simply representing an
                # empty POST
                self._mark_post_parse_error()
                raise
        elif self.META.get('CONTENT_TYPE', '').startswith('application/x-www-form-urlencoded'):
            self._post, self._files = QueryDict(self.body, encoding=self._encoding), MultiValueDict()
        else:
            self._post, self._files = QueryDict('', encoding=self._encoding), MultiValueDict()

    def close(self):
        if hasattr(self, '_files'):
            for f in chain.from_iterable(l[1] for l in self._files.lists()):
                f.close()

    # 类文件和迭代器接口。
    # 期望self._stream被相应的请求子类（例如WSGIRequest）设置为适当的字节源。
    # 当请求数据已被request.POST或request.body读取时，self._stream指向包含该数据的BytesIO实例。

    def read(self, *args, **kwargs):
        self._read_started = True
        try:
            return self._stream.read(*args, **kwargs)
        except IOError as e:
            six.reraise(UnreadablePostError, UnreadablePostError(*e.args), sys.exc_info()[2])

    def readline(self, *args, **kwargs):
        self._read_started = True
        try:
            return self._stream.readline(*args, **kwargs)
        except IOError as e:
            six.reraise(UnreadablePostError, UnreadablePostError(*e.args), sys.exc_info()[2])

    def xreadlines(self):
        while True:
            buf = self.readline()
            if not buf:
                break
            yield buf

    __iter__ = xreadlines

    def readlines(self):
        return list(iter(self))


class QueryDict(MultiValueDict):
    """
    表示查询字符串的专用MultiValueDict。

    QueryDict可以用来表示GET或POST数据。 
    它是MultiValueDict的子类，因为可以重复这些数据中的键，
    例如来自具有<select multiple>字段的表单中的数据。

    默认情况下QueryDicts是不可变的，尽管copy()方法总是返回一个可变副本。

    在这个类上设置的键和值都是从给定的编码（默认情况下为DEFAULT_CHARSET）转换为unicode。
    """

    # 这些都在__init__中重置，但是在类级别处指定，以便取消打印将具有有效值
    _mutable = True
    _encoding = None

    def __init__(self, query_string=None, mutable=False, encoding=None):
        super(QueryDict, self).__init__()
        if not encoding:
            encoding = settings.DEFAULT_CHARSET
        self.encoding = encoding
        if six.PY3:
            if isinstance(query_string, bytes):
                # query_string通常包含URL编码的数据，即ASCII的一个子集。
                try:
                    query_string = query_string.decode(encoding)
                except UnicodeDecodeError:
                    # ...但一些用户代理行为异常:-(
                    query_string = query_string.decode('iso-8859-1')
            for key, value in parse_qsl(query_string or '',
                                        keep_blank_values=True,
                                        encoding=encoding):
                self.appendlist(key, value)
        else:
            for key, value in parse_qsl(query_string or '',
                                        keep_blank_values=True):
                try:
                    value = value.decode(encoding)
                except UnicodeDecodeError:
                    value = value.decode('iso-8859-1')
                self.appendlist(force_text(key, encoding, errors='replace'),
                                value)
        self._mutable = mutable

    @property
    def encoding(self):
        if self._encoding is None:
            self._encoding = settings.DEFAULT_CHARSET
        return self._encoding

    @encoding.setter
    def encoding(self, value):
        self._encoding = value

    def _assert_mutable(self):
        if not self._mutable:
            raise AttributeError("This QueryDict instance is immutable")

    def __setitem__(self, key, value):
        self._assert_mutable()
        key = bytes_to_text(key, self.encoding)
        value = bytes_to_text(value, self.encoding)
        super(QueryDict, self).__setitem__(key, value)

    def __delitem__(self, key):
        self._assert_mutable()
        super(QueryDict, self).__delitem__(key)

    def __copy__(self):
        result = self.__class__('', mutable=True, encoding=self.encoding)
        for key, value in six.iterlists(self):
            result.setlist(key, value)
        return result

    def __deepcopy__(self, memo):
        result = self.__class__('', mutable=True, encoding=self.encoding)
        memo[id(self)] = result
        for key, value in six.iterlists(self):
            result.setlist(copy.deepcopy(key, memo), copy.deepcopy(value, memo))
        return result

    def setlist(self, key, list_):
        self._assert_mutable()
        key = bytes_to_text(key, self.encoding)
        list_ = [bytes_to_text(elt, self.encoding) for elt in list_]
        super(QueryDict, self).setlist(key, list_)

    def setlistdefault(self, key, default_list=None):
        self._assert_mutable()
        return super(QueryDict, self).setlistdefault(key, default_list)

    def appendlist(self, key, value):
        self._assert_mutable()
        key = bytes_to_text(key, self.encoding)
        value = bytes_to_text(value, self.encoding)
        super(QueryDict, self).appendlist(key, value)

    def pop(self, key, *args):
        self._assert_mutable()
        return super(QueryDict, self).pop(key, *args)

    def popitem(self):
        self._assert_mutable()
        return super(QueryDict, self).popitem()

    def clear(self):
        self._assert_mutable()
        super(QueryDict, self).clear()

    def setdefault(self, key, default=None):
        self._assert_mutable()
        key = bytes_to_text(key, self.encoding)
        default = bytes_to_text(default, self.encoding)
        return super(QueryDict, self).setdefault(key, default)

    def copy(self):
        """返回此对象的可变副本。"""
        return self.__deepcopy__({})

    def urlencode(self, safe=None):
        """
        返回所有查询字符串参数的编码字符串。

         ：arg safe：用于指定不需要引用的字符
             例：

                >>> q = QueryDict('', mutable=True)
                >>> q['next'] = '/a&b/'
                >>> q.urlencode()
                'next=%2Fa%26b%2F'
                >>> q.urlencode(safe='/')
                'next=/a%26b/'

        """
        output = []
        if safe:
            safe = force_bytes(safe, self.encoding)
            encode = lambda k, v: '%s=%s' % ((quote(k, safe), quote(v, safe)))
        else:
            encode = lambda k, v: urlencode({k: v})
        for k, list_ in self.lists():
            k = force_bytes(k, self.encoding)
            output.extend(encode(k, force_bytes(v, self.encoding))
                          for v in list_)
        return '&'.join(output)


def build_request_repr(request, path_override=None, GET_override=None,
                       POST_override=None, COOKIES_override=None,
                       META_override=None):
    """
    Builds and returns the request's representation string. The request's
    attributes may be overridden by pre-processed values.
    """
    # Since this is called as part of error handling, we need to be very
    # robust against potentially malformed input.
    try:
        get = (pformat(GET_override)
               if GET_override is not None
               else pformat(request.GET))
    except Exception:
        get = '<could not parse>'
    if request._post_parse_error:
        post = '<could not parse>'
    else:
        try:
            post = (pformat(POST_override)
                    if POST_override is not None
                    else pformat(request.POST))
        except Exception:
            post = '<could not parse>'
    try:
        cookies = (pformat(COOKIES_override)
                   if COOKIES_override is not None
                   else pformat(request.COOKIES))
    except Exception:
        cookies = '<could not parse>'
    try:
        meta = (pformat(META_override)
                if META_override is not None
                else pformat(request.META))
    except Exception:
        meta = '<could not parse>'
    path = path_override if path_override is not None else request.path
    return force_str('<%s\npath:%s,\nGET:%s,\nPOST:%s,\nCOOKIES:%s,\nMETA:%s>' %
                     (request.__class__.__name__,
                      path,
                      six.text_type(get),
                      six.text_type(post),
                      six.text_type(cookies),
                      six.text_type(meta)))


# It's neither necessary nor appropriate to use
# django.utils.encoding.smart_text for parsing URLs and form inputs. Thus,
# this slightly more restricted function, used by QueryDict.
def bytes_to_text(s, encoding):
    """
    Converts basestring objects to unicode, using the given encoding. Illegally
    encoded input characters are replaced with Unicode "unknown" codepoint
    (\ufffd).

    Returns any non-basestring objects without change.
    """
    if isinstance(s, bytes):
        return six.text_type(s, encoding, 'replace')
    else:
        return s


def split_domain_port(host):
    """
    Return a (domain, port) tuple from a given host.

    Returned domain is lower-cased. If the host is invalid, the domain will be
    empty.
    """
    host = host.lower()

    if not host_validation_re.match(host):
        return '', ''

    if host[-1] == ']':
        # It's an IPv6 address without a port.
        return host, ''
    bits = host.rsplit(':', 1)
    if len(bits) == 2:
        return tuple(bits)
    return bits[0], ''


def validate_host(host, allowed_hosts):
    """
    Validate the given host for this site.

    Check that the host looks valid and matches a host or host pattern in the
    given list of ``allowed_hosts``. Any pattern beginning with a period
    matches a domain and all its subdomains (e.g. ``.example.com`` matches
    ``example.com`` and any subdomain), ``*`` matches anything, and anything
    else must match exactly.

    Note: This function assumes that the given host is lower-cased and has
    already had the port, if any, stripped off.

    Return ``True`` for a valid host, ``False`` otherwise.

    """
    host = host[:-1] if host.endswith('.') else host

    for pattern in allowed_hosts:
        pattern = pattern.lower()
        match = (
            pattern == '*' or
            pattern.startswith('.') and (
                host.endswith(pattern) or host == pattern[1:]
            ) or
            pattern == host
        )
        if match:
            return True

    return False
