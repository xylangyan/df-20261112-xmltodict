#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from io import StringIO
from inspect import isgenerator
import codecs
import re

class ParsingInterrupted(Exception):
    pass


class SourceLocation(dict):
    """A mapping of source-position keys that also supports attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class XMLParseError(Exception):
    """Base class for XML parsing errors that carry source context."""

    def __init__(self, message, context):
        super().__init__(message)
        self.context = SourceLocation(context)
        self.location = self.context
        self.record_index = self.context.get("record_index")
        self.item_index = self.record_index
        self.element_path = self.context.get("element_path")
        self.path = self.element_path
        self.field_name = self.context.get("field_name")
        self.line = self.context.get("line")
        self.line_number = self.line
        self.column = self.context.get("column")
        self.column_number = self.column
        self.byte_index = self.context.get("byte_index")
        self.byte_offset = self.byte_index
        self.position = self.byte_index
        self.position_kind = self.context.get("position_kind")
        self.target = self.position_kind

    def __str__(self):
        details = [super().__str__()]
        if self.record_index is not None:
            details.append(f"record_index={self.record_index}")
        if self.element_path is not None:
            details.append(f"element_path={self.element_path}")
        if self.field_name is not None:
            details.append(f"field_name={self.field_name}")
        if self.line is not None:
            details.append(f"line={self.line}")
        if self.column is not None:
            details.append(f"column={self.column}")
        if self.byte_index is not None:
            details.append(f"byte_index={self.byte_index}")
        if self.position_kind is not None:
            details.append(f"position_kind={self.position_kind}")
        if len(details) > 1:
            return f"{details[0]} ({', '.join(details[1:])})"
        return details[0]


class XMLPostprocessorError(XMLParseError, ValueError):
    """A postprocessor failed while processing an XML value or attribute.

    It also inherits from ``ValueError`` so existing handlers that catch the
    postprocessor's original exception type keep working.
    """


class XMLSyntaxError(XMLParseError, expat.ExpatError):
    """The XML parser rejected malformed source XML."""


# Common alternative spellings kept as aliases for callers that prefer the
# shorter names.
XMLParsingError = XMLParseError
PostprocessorError = XMLPostprocessorError
XMLParserError = XMLSyntaxError


class _DictSAXHandler:
    def __init__(
        self,
        item_depth=0,
        item_callback=lambda *args: True,
        xml_attribs=True,
        attr_prefix="@",
        cdata_key="#text",
        force_cdata=False,
        cdata_separator="",
        postprocessor=None,
        dict_constructor=dict,
        strip_whitespace=True,
        namespace_separator=":",
        namespaces=None,
        force_list=None,
        comment_key="#comment",
        track_locations=False,
    ):
        self.path = []
        self.stack = []
        self.data = []
        self.item = None
        self.track_locations = track_locations
        self._parser = None
        self._source_prefix = None
        self._data_locations = []
        self._element_locations = []
        self._record_index = 0
        self._last_event_was_characters = False
        self.item_depth = item_depth
        self.xml_attribs = xml_attribs
        self.item_callback = item_callback
        self.attr_prefix = attr_prefix
        self.cdata_key = cdata_key
        self.force_cdata = force_cdata
        self.cdata_separator = cdata_separator
        self.postprocessor = postprocessor
        self.dict_constructor = dict_constructor
        self.strip_whitespace = strip_whitespace
        self.namespace_separator = namespace_separator
        self.namespaces = namespaces
        self.namespace_declarations = dict_constructor()
        self.force_list = force_list
        self.comment_key = comment_key

    def _build_name(self, full_name):
        if self.namespaces is None:
            return full_name
        i = full_name.rfind(self.namespace_separator)
        if i == -1:
            return full_name
        namespace, name = full_name[:i], full_name[i+1:]
        try:
            short_namespace = self.namespaces[namespace]
        except KeyError:
            short_namespace = namespace
        if not short_namespace:
            return name
        else:
            return self.namespace_separator.join((short_namespace, name))

    def _attrs_to_dict(self, attrs):
        if isinstance(attrs, dict):
            return attrs
        return self.dict_constructor(zip(attrs[0::2], attrs[1::2]))

    def _current_location(self):
        if self._parser is None:
            return None
        index = self._parser.CurrentByteIndex
        return (
            self._parser.CurrentLineNumber,
            self._parser.CurrentColumnNumber + 1,
            self._source_byte_index(index),
        )

    def _source_byte_index(self, parser_index):
        if not self._source_prefix:
            return parser_index
        if 0 <= parser_index <= len(self._source_prefix):
            return parser_index
        # Start tags split across input chunks can reset expat's byte index to
        # the start of the buffered token. The input context is the source
        # prefix in that case, and raw byte positions are exact.
        context = self._parser.GetInputContext()
        if not context:
            return parser_index
        window_start = self._source_prefix.rfind(context)
        if window_start < 0:
            return parser_index
        return window_start + parser_index

    def _element_path(self):
        return tuple(name for name, _attrs in self.path)

    def _context(self, key=None, location=None, position_kind=None):
        context = {
            "record_index": self._record_index or None,
            "element_path": self._element_path(),
        }
        if key is not None:
            context["field_name"] = key
        if location is not None:
            context["line"], context["column"], context["byte_index"] = location
        if position_kind is not None:
            context["position_kind"] = position_kind
        return context

    def _attribute_location(self, name, element_location):
        if self._parser is None or element_location is None:
            return None
        context = self._parser.GetInputContext()
        if not context:
            return None
        names = [name]
        if self.namespaces is not None and ":" in name:
            names.append(name.rsplit(":", 1)[-1])
        match = None
        for candidate_name in names:
            pattern = re.compile(
                rb"(?:\s|^)" + re.escape(candidate_name.encode("utf-8"))
                + rb"\s*=\s*(['\"])"
            )
            for candidate in pattern.finditer(context):
                prefix = context[:candidate.start()]
                if prefix.count(b'"') % 2 == 0 and prefix.count(b"'") % 2 == 0:
                    match = candidate
                    break
            if match is not None:
                break
        if match is None:
            return None
        value_index = match.end()
        tag_index = context.find(b"<")
        if tag_index < 0:
            tag_index = 0
        try:
            prefix = context[tag_index:value_index].decode("utf-8")
        except UnicodeDecodeError:
            prefix = context[tag_index:value_index]
        line_start = max(prefix.rfind("\n"), prefix.rfind("\r"))
        line = element_location[0] + prefix.count("\n")
        if line_start < 0:
            column = element_location[1] + len(prefix)
        else:
            column = len(prefix) - line_start
        byte_index = element_location[2] - tag_index + value_index
        return (line, column, byte_index)

    def _raise_postprocessor_error(self, exc, key, location, position_kind):
        context = self._context(key, location, position_kind)
        message = (
            f"postprocessor failed for field {key!r} "
            f"at element path {'/'.join(context['element_path'])!r}"
        )
        raise XMLPostprocessorError(message, context) from exc

    def startNamespaceDecl(self, prefix, uri):
        self.namespace_declarations[prefix or ''] = uri

    def startElement(self, full_name, attrs):
        name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        element_location = self._current_location()
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = self.namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        self.path.append((name, attrs or None))
        self._element_locations.append(element_location)
        self._data_locations.append(None)
        if len(self.path) == self.item_depth and self.item_depth:
            self._record_index += 1
        self._last_event_was_characters = False
        if len(self.path) >= self.item_depth:
            self.stack.append((self.item, self.data))
            if self.xml_attribs:
                attr_entries = []
                for raw_key, value in attrs.items():
                    key = self.attr_prefix+self._build_name(raw_key)
                    if self.postprocessor:
                        attribute_location = self._attribute_location(
                            raw_key, element_location or (None, None, None))
                        try:
                            entry = self.postprocessor(self.path, key, value)
                        except Exception as exc:
                            if self.track_locations:
                                self._raise_postprocessor_error(
                                    exc, key, attribute_location, "attribute_value")
                            raise
                    else:
                        entry = (key, value)
                    if entry:
                        attr_entries.append(entry)
                attrs = self.dict_constructor(attr_entries)
            else:
                attrs = None
            self.item = attrs or None
            self.data = []

    def endElement(self, full_name):
        name = self._build_name(full_name)
        element_location = self._element_locations.pop()
        self._last_event_was_characters = False
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            data_location = self._data_locations[-1]
            item = self.item
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data, data_location,
                                   "value")
            else:
                item = data

            should_continue = self.item_callback(self.path, item)
            if not should_continue:
                raise ParsingInterrupted
            # Reset state for the parent context without keeping a reference to
            # the emitted item.
            if self.stack:
                self.item, self.data = self.stack.pop()
                self._data_locations.pop()
            else:
                self.item = None
                self.data = []
                self._data_locations = []
            self.path.pop()
            return
        if self.stack:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            data_location = self._data_locations[-1]
            item = self.item
            self.item, self.data = self.stack.pop()
            self._data_locations.pop()
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data, data_location,
                                   "value")
                self.item = self.push_data(self.item, name, item,
                                           element_location, "start_tag")
            else:
                self.item = self.push_data(self.item, name, data,
                                           data_location, "value")
        else:
            self.item = None
            self.data = []
            self._data_locations = []
        self.path.pop()

    def characters(self, data):
        location = self._current_location()
        if not self.data:
            self.data = [data]
            self._data_locations[-1] = location
        elif self._last_event_was_characters:
            self.data[-1] += data
        else:
            self.data.append(data)
            self._data_locations[-1] = location
        self._last_event_was_characters = True

    def comments(self, data):
        self._last_event_was_characters = False
        if self.strip_whitespace:
            data = data.strip()
        self.item = self.push_data(self.item, self.comment_key, data,
                                   self._current_location(), "value")

    def push_data(self, item, key, data, location=None, position_kind=None):
        if self.postprocessor is not None:
            try:
                result = self.postprocessor(self.path, key, data)
            except Exception as exc:
                if self.track_locations:
                    self._raise_postprocessor_error(
                        exc, key, location, position_kind)
                raise
            if result is None:
                return item
            key, data = result
        if item is None:
            item = self.dict_constructor()
        try:
            value = item[key]
            if isinstance(value, list):
                value.append(data)
            else:
                item[key] = [value, data]
        except KeyError:
            if self._should_force_list(key, data):
                item[key] = [data]
            else:
                item[key] = data
        return item

    def _should_force_list(self, key, value):
        if not self.force_list:
            return False
        if isinstance(self.force_list, bool):
            return self.force_list
        try:
            return key in self.force_list
        except TypeError:
            return self.force_list(self.path[:-1], key, value)

    def _should_force_cdata(self, key, value):
        if not self.force_cdata:
            return False
        if isinstance(self.force_cdata, bool):
            return self.force_cdata
        try:
            return key in self.force_cdata
        except TypeError:
            return self.force_cdata(self.path[:-1], key, value)


def parse(xml_input, encoding=None, expat=expat, process_namespaces=False,
          namespace_separator=':', disable_entities=True, process_comments=False,
          error_context=False, source_context=None, include_error_context=None,
          **kwargs):
    """Parse the given XML input and convert it into a dictionary.

    `xml_input` can either be a `string`, a file-like object, or a generator of strings.

    If `xml_attribs` is `True`, element attributes are put in the dictionary
    among regular child elements, using `@` as a prefix to avoid collisions. If
    set to `False`, they are just ignored.

    Simple example::

        >>> import xmltodict
        >>> doc = xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>
        ... \"\"\")
        >>> doc['a']['@prop']
        'x'
        >>> doc['a']['b']
        ['1', '2']

    If `item_depth` is `0`, the function returns a dictionary for the root
    element (default behavior). Otherwise, it calls `item_callback` every time
    an item at the specified depth is found and returns `None` in the end
    (streaming mode).

    The callback function receives two parameters: the `path` from the document
    root to the item (name-attribs pairs), and the `item` (dict). If the
    callback's return value is false-ish, parsing will be stopped with the
    :class:`ParsingInterrupted` exception.

    Streaming example::

        >>> def handle(path, item):
        ...     print('path:%s item:%s' % (path, item))
        ...     return True
        ...
        >>> xmltodict.parse(\"\"\"
        ... <a prop="x">
        ...   <b>1</b>
        ...   <b>2</b>
        ... </a>\"\"\", item_depth=2, item_callback=handle)
        path:[('a', {'prop': 'x'}), ('b', None)] item:1
        path:[('a', {'prop': 'x'}), ('b', None)] item:2

    The optional argument `postprocessor` is a function that takes `path`,
    `key` and `value` as positional arguments and returns a new `(key, value)`
    pair where both `key` and `value` may have changed. Usage example::

        >>> def postprocessor(path, key, value):
        ...     try:
        ...         return key + ':int', int(value)
        ...     except (ValueError, TypeError):
        ...         return key, value
        >>> xmltodict.parse('<a><b>1</b><b>2</b><b>x</b></a>',
        ...                 postprocessor=postprocessor)
        {'a': {'b:int': [1, 2], 'b': 'x'}}

    Set `error_context=True` to wrap postprocessor failures in
    :class:`XMLPostprocessorError` and malformed XML in
    :class:`XMLSyntaxError`. The `context` attribute contains the one-based
    record number for streaming items, the element path, field name,
    one-based line and column, source byte offset, and whether the position
    identifies an element start tag, value, or the parser error location.

    You can pass an alternate version of `expat` (such as `defusedexpat`) by
    using the `expat` parameter. E.g:

        >>> import defusedexpat
        >>> xmltodict.parse('<a>hello</a>', expat=defusedexpat.pyexpat)
        {'a': 'hello'}

    You can use the force_list argument to force lists to be created even
    when there is only a single child of a given level of hierarchy. The
    force_list argument is a tuple of keys. If the key for a given level
    of hierarchy is in the force_list argument, that level of hierarchy
    will have a list as a child (even if there is only one sub-element).
    The index_keys operation takes precedence over this. This is applied
    after any user-supplied postprocessor has already run.

        For example, given this input:
        <servers>
          <server>
            <name>host1</name>
            <os>Linux</os>
            <interfaces>
              <interface>
                <name>em0</name>
                <ip_address>10.0.0.1</ip_address>
              </interface>
            </interfaces>
          </server>
        </servers>

        If called with force_list=('interface',), it will produce
        this dictionary:
        {'servers':
          {'server':
            {'name': 'host1',
             'os': 'Linux'},
             'interfaces':
              {'interface':
                [ {'name': 'em0', 'ip_address': '10.0.0.1' } ] } } }

        `force_list` can also be a callable that receives `path`, `key` and
        `value`. This is helpful in cases where the logic that decides whether
        a list should be forced is more complex.


        If `process_comments` is `True`, comments will be added using `comment_key`
        (default=`'#comment'`) to the tag that contains the comment.

            For example, given this input:
            <a>
              <b>
                <!-- b comment -->
                <c>
                    <!-- c comment -->
                    1
                </c>
                <d>2</d>
              </b>
            </a>

            If called with `process_comments=True`, it will produce
            this dictionary:
            'a': {
                'b': {
                    '#comment': 'b comment',
                    'c': {

                        '#comment': 'c comment',
                        '#text': '1',
                    },
                    'd': '2',
                },
            }
        Comment text is subject to the `strip_whitespace` flag: when it is left
        at the default `True`, comments will have leading and trailing
        whitespace removed. Disable `strip_whitespace` to keep comment
        indentation or padding intact.
    """
    track_locations = bool(error_context)
    if source_context is not None:
        track_locations = bool(source_context)
    if include_error_context is not None:
        track_locations = bool(include_error_context)
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              track_locations=track_locations,
                              **kwargs)
    if isinstance(xml_input, str):
        encoding = encoding or 'utf-8'
        xml_input = xml_input.encode(encoding)
    if not process_namespaces:
        namespace_separator = None
    parser = expat.ParserCreate(
        encoding,
        namespace_separator
    )
    handler._parser = parser
    if handler.track_locations:
        if isinstance(xml_input, (bytes, bytearray, memoryview)):
            handler._source_prefix = bytearray(xml_input)
    parser.ordered_attributes = True
    parser.StartNamespaceDeclHandler = handler.startNamespaceDecl
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    if process_comments:
        parser.CommentHandler = handler.comments
    # Buffered text does not expose the position where character data starts.
    # Disable it only for location tracking; character events are coalesced in
    # the handler so the parsed result stays the same.
    parser.buffer_text = not handler.track_locations
    if disable_entities:
        def _forbid_entities(*_args, **_kwargs):
            raise ValueError("entities are disabled")

        parser.EntityDeclHandler = _forbid_entities

    def _parse_with_context(parse):
        if not handler.track_locations:
            return parse()
        try:
            return parse()
        except expat.ExpatError as exc:
            location = handler._current_location()
            context = handler._context(
                location=location, position_kind="parser_error")
            wrapped = XMLSyntaxError(str(exc), context)
            wrapped.__cause__ = exc
            for attr in ("code", "lineno", "offset"):
                try:
                    setattr(wrapped, attr, getattr(exc, attr))
                except AttributeError:
                    pass
            raise wrapped from exc

    if hasattr(xml_input, 'read'):
        _parse_with_context(lambda: parser.ParseFile(xml_input))
    elif isgenerator(xml_input):
        def _parse_chunks():
            for chunk in xml_input:
                if handler.track_locations and isinstance(chunk, str):
                    if handler._source_prefix is None:
                        handler._source_prefix = bytearray()
                    handler._source_prefix += chunk.encode("utf-8")
                parser.Parse(chunk, False)
            parser.Parse(b'', True)

        _parse_with_context(_parse_chunks)
    else:
        _parse_with_context(lambda: parser.Parse(xml_input, True))
    return handler.item


def _convert_value_to_string(value, encoding='utf-8', bytes_errors='replace'):
    """Convert a value to its string representation for XML output.

    Handles boolean values consistently by converting them to lowercase.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode(encoding, errors=bytes_errors)
    return str(value)


def _validate_name(value, kind):
    """Validate an element/attribute name for XML safety.

    Raises ValueError with a specific reason when invalid.

    kind: 'element' or 'attribute' (used in error messages)
    """
    if not isinstance(value, str):
        raise ValueError(f"{kind} name must be a string")
    if value.startswith("?") or value.startswith("!"):
        raise ValueError(f'Invalid {kind} name: cannot start with "?" or "!"')
    if "<" in value or ">" in value:
        raise ValueError(f'Invalid {kind} name: "<" or ">" not allowed')
    if "/" in value:
        raise ValueError(f'Invalid {kind} name: "/" not allowed')
    if '"' in value or "'" in value:
        raise ValueError(f"Invalid {kind} name: quotes not allowed")
    if "=" in value:
        raise ValueError(f'Invalid {kind} name: "=" not allowed')
    if any(ch.isspace() for ch in value):
        raise ValueError(f"Invalid {kind} name: whitespace not allowed")


def _validate_comment(value):
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Comment text must be valid UTF-8") from exc
    if not isinstance(value, str):
        raise ValueError("Comment text must be a string")
    if "--" in value:
        raise ValueError("Comment text cannot contain '--'")
    if value.endswith("-"):
        raise ValueError("Comment text cannot end with '-'")
    return value


def _process_namespace(name, namespaces, ns_sep=':', attr_prefix='@'):
    if not isinstance(name, str):
        return name
    if not namespaces:
        return name
    try:
        ns, name = name.rsplit(ns_sep, 1)
    except ValueError:
        pass
    else:
        ns_res = namespaces.get(ns.strip(attr_prefix))
        name = '{}{}{}{}'.format(
            attr_prefix if ns.startswith(attr_prefix) else '',
            ns_res, ns_sep, name) if ns_res else name
    return name


def _emit(key, value, content_handler,
          attr_prefix='@',
          cdata_key='#text',
          depth=0,
          preprocessor=None,
          pretty=False,
          newl='\n',
          indent='\t',
          namespace_separator=':',
          namespaces=None,
          full_document=True,
          expand_iter=None,
          encoding='utf-8',
          bytes_errors='replace',
          comment_key='#comment'):
    if isinstance(key, str) and key == comment_key:
        comments_list = value if isinstance(value, list) else [value]
        if isinstance(indent, int):
            indent = " " * indent
        for comment_text in comments_list:
            if comment_text is None:
                continue
            comment_text = _convert_value_to_string(
                comment_text, encoding=encoding, bytes_errors=bytes_errors
            )
            if not comment_text:
                continue
            if pretty:
                content_handler.ignorableWhitespace(depth * indent)
            content_handler.comment(comment_text)
            if pretty:
                content_handler.ignorableWhitespace(newl)
        return

    key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    _validate_name(key, "element")
    if not hasattr(value, '__iter__') or isinstance(value, (str, bytes, bytearray, memoryview, dict)):
        value = [value]
    for index, v in enumerate(value):
        if full_document and depth == 0 and index > 0:
            raise ValueError('document with multiple roots')
        if v is None:
            v = {}
        elif not isinstance(v, (dict, str)):
            if expand_iter and hasattr(v, '__iter__') and not isinstance(v, (bytes, bytearray, memoryview)):
                v = {expand_iter: v}
            else:
                v = _convert_value_to_string(v, encoding=encoding, bytes_errors=bytes_errors)
        if isinstance(v, str):
            v = {cdata_key: v}
        cdata = None
        attrs = {}
        children = []
        for ik, iv in v.items():
            if ik == cdata_key:
                if iv is None:
                    cdata = None
                else:
                    cdata = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                continue
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                ik = _process_namespace(ik, namespaces, namespace_separator,
                                        attr_prefix)
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for k, v in iv.items():
                        _validate_name(k, "attribute")
                        attr = 'xmlns{}'.format(f':{k}' if k else '')
                        attrs[attr] = '' if v is None else _convert_value_to_string(
                            v, encoding=encoding, bytes_errors=bytes_errors
                        )
                    continue
                if iv is None:
                    iv = ''
                elif not isinstance(iv, str):
                    iv = _convert_value_to_string(iv, encoding=encoding, bytes_errors=bytes_errors)
                attr_name = ik[len(attr_prefix) :]
                _validate_name(attr_name, "attribute")
                attrs[attr_name] = iv
                continue
            if isinstance(iv, list) and not iv:
                continue # Skip empty lists to avoid creating empty child elements
            children.append((ik, iv))
        if isinstance(indent, int):
            indent = ' ' * indent
        if pretty:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.startElement(key, AttributesImpl(attrs))
        if pretty and children:
            content_handler.ignorableWhitespace(newl)
        for child_key, child_value in children:
            _emit(child_key, child_value, content_handler,
                  attr_prefix, cdata_key, depth+1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(key)
        if pretty and depth:
            content_handler.ignorableWhitespace(newl)


class _XMLGenerator(XMLGenerator):
    def comment(self, text):
        text = _validate_comment(text)
        self._write(f"<!--{escape(text)}-->")


def unparse(input_dict, output=None, encoding='utf-8', full_document=True,
            short_empty_elements=False, comment_key='#comment',
            **kwargs):
    """Emit an XML document for the given `input_dict` (reverse of `parse`).

    The resulting XML document is returned as a string, but if `output` (a
    file-like object) is specified, it is written there instead.

    Dictionary keys prefixed with `attr_prefix` (default=`'@'`) are interpreted
    as XML node attributes, whereas keys equal to `cdata_key`
    (default=`'#text'`) are treated as character data.

    Empty lists are omitted entirely: ``{"a": []}`` produces no ``<a>`` element.
    Provide a placeholder entry (for example ``{"a": [""]}``) when an explicit
    empty container element must be emitted.

    The `pretty` parameter (default=`False`) enables pretty-printing. In this
    mode, lines are terminated with `'\n'` and indented with `'\t'`, but this
    can be customized with the `newl` and `indent` parameters.
    The `bytes_errors` parameter controls decoding errors for byte values and
    defaults to `'replace'`.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc

    must_return = False
    if output is None:
        output = StringIO()
        must_return = True
    if short_empty_elements:
        content_handler = _XMLGenerator(output, encoding, True)
    else:
        content_handler = _XMLGenerator(output, encoding)
    if full_document:
        content_handler.startDocument()
    seen_root = False
    for key, value in input_dict.items():
        if key != comment_key and full_document and seen_root:
            raise ValueError("Document must have exactly one root.")
        _emit(
            key,
            value,
            content_handler,
            full_document=full_document,
            encoding=encoding,
            bytes_errors=bytes_errors,
            comment_key=comment_key,
            **kwargs,
        )
        if key != comment_key:
            seen_root = True
    if full_document and not seen_root:
        raise ValueError("Document must have exactly one root.")
    if full_document:
        content_handler.endDocument()
    if must_return:
        value = output.getvalue()
        try:  # pragma no cover
            value = value.decode(encoding)
        except AttributeError:  # pragma no cover
            pass
        return value


if __name__ == '__main__':  # pragma: no cover
    import marshal
    import sys

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    (item_depth,) = sys.argv[1:]
    item_depth = int(item_depth)

    def handle_item(path, item):
        marshal.dump((path, item), stdout)
        return True

    try:
        root = parse(stdin,
                     item_depth=item_depth,
                     item_callback=handle_item,
                     dict_constructor=dict)
        if item_depth == 0:
            handle_item([], root)
    except KeyboardInterrupt:
        pass
