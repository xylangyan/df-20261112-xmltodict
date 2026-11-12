#!/usr/bin/env python
"Makes working with XML feel like you are working with JSON"

from xml.parsers import expat
from xml.sax.saxutils import XMLGenerator, escape
from xml.sax.xmlreader import AttributesImpl
from io import StringIO
from inspect import isgenerator
import codecs

class ParsingInterrupted(Exception):
    pass


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
        preserve_order=False,
    ):
        self.path = []
        self.stack = []
        self.data = []
        self.item = None
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
        self.preserve_order = preserve_order

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

    def startNamespaceDecl(self, prefix, uri):
        self.namespace_declarations[prefix or ''] = uri

    def startElement(self, full_name, attrs):
        name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        if self.namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = self.namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        self.path.append((name, attrs or None))
        if len(self.path) >= self.item_depth:
            if self.preserve_order:
                # Third slot holds the ordered event stream for this element.
                self.stack.append((self.item, self.data, []))
            else:
                self.stack.append((self.item, self.data))
            if self.xml_attribs:
                attr_entries = []
                for key, value in attrs.items():
                    key = self.attr_prefix+self._build_name(key)
                    if self.postprocessor:
                        entry = self.postprocessor(self.path, key, value)
                    else:
                        entry = (key, value)
                    if entry:
                        attr_entries.append(entry)
                attrs = self.dict_constructor(attr_entries)
            else:
                attrs = None
            self.item = attrs or None
            self.data = []

    def _merge_text_events(self, events):
        # Adjacent text fragments are delivered by expat as separate chunks;
        # merge them into one text event using cdata_separator, and drop text
        # that whitespace stripping (or a separator-only join) reduces to
        # nothing, matching the plain-dict representation.
        merged = []
        for event in events:
            if event[0] == 'text':
                if merged and merged[-1][0] == 'text':
                    merged[-1] = ('text',
                                  merged[-1][1] + self.cdata_separator
                                  + event[1])
                else:
                    merged.append(event)
            else:
                merged.append(event)
        return [event for event in merged
                if not (event[0] == 'text' and not event[1])]

    def _flush_pending_text(self, parent_events, saved_data):
        # Text accumulated by a parent element before this child must be
        # recorded ahead of the child's element event. Whitespace handling
        # mirrors the plain-dict path: only fully whitespace-only chunks are
        # dropped when strip_whitespace is enabled.
        if not saved_data:
            return
        pending = self.cdata_separator.join(saved_data)
        if self.strip_whitespace and not pending.strip():
            return
        parent_events.append(('text', pending))

    def _finalize_ordered_item(self, item, data, name, events):
        if data:
            events.append(('text', data))
        events = self._merge_text_events(events)
        if item is None and not events:
            return None
        if item is None:
            item = self.dict_constructor()
        if data and self._should_force_cdata(name, data) and self.cdata_key not in item:
            item[self.cdata_key] = data
        item['#events'] = events
        return item

    def endElement(self, full_name):
        name = self._build_name(full_name)
        data = (None if not self.data
                else self.cdata_separator.join(self.data))
        if self.strip_whitespace and data:
            data = data.strip() or None
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            item = self.item
            if self.preserve_order:
                if data and self._should_force_cdata(name, data) and item is None:
                    item = self.dict_constructor()
                _saved_item, _saved_data, own_events = self.stack[-1]
                item = self._finalize_ordered_item(item, data, name,
                                                   own_events)
            else:
                if data and self._should_force_cdata(name, data) and item is None:
                    item = self.dict_constructor()
                if item is not None:
                    if data:
                        self.push_data(item, self.cdata_key, data)
                else:
                    item = data

            should_continue = self.item_callback(self.path, item)
            if not should_continue:
                raise ParsingInterrupted
            # Reset state for the parent context without keeping a reference to
            # the emitted item.
            if self.stack:
                if self.preserve_order:
                    self.item, self.data, _events = self.stack.pop()
                else:
                    self.item, self.data = self.stack.pop()
            else:
                self.item = None
                self.data = []
            self.path.pop()
            return
        if self.stack:
            item = self.item
            if self.preserve_order:
                # The frame pushed by this element's startElement is the last
                # one on the stack; alongside the saved parent state it holds
                # this element's ordered event list.
                saved_item, saved_data, own_events = self.stack.pop()
                if self.stack:
                    self._flush_pending_text(self.stack[-1][2], saved_data)
                item = self._finalize_ordered_item(item, data, name,
                                                   own_events)
                if self.stack:
                    # Keep the plain-dict mapping in sync (repeated tags still
                    # collapse into lists, etc.) while the event list keeps
                    # order. The postprocessor applies to both views at once.
                    if self.postprocessor is not None:
                        result = self.postprocessor(self.path, name, item)
                        if result is None:
                            self.item = saved_item
                            self.data = []
                            self.path.pop()
                            return
                        name, item = result
                    self.item = self._push_data(saved_item, name, item)
                    self.data = []
                    self.stack[-1][2].append(('element', name, item))
                else:
                    # Root element: wrap it under its tag name, like the
                    # plain-dict representation.
                    self.item = self.dict_constructor(((name, item),))
                    self.data = []
            else:
                self.item, self.data = self.stack.pop()
                if data and self._should_force_cdata(name, data) and item is None:
                    item = self.dict_constructor()
                if item is not None:
                    if data:
                        self.push_data(item, self.cdata_key, data)
                    self.item = self.push_data(self.item, name, item)
                else:
                    self.item = self.push_data(self.item, name, data)
        else:
            # No frame was ever pushed: item_depth lies below the document, so
            # nothing was streamed and the result is None in either mode.
            self.item = None
            self.data = []
        self.path.pop()

    def characters(self, data):
        if not self.data:
            self.data = [data]
        else:
            self.data.append(data)

    def comments(self, data):
        if self.strip_whitespace:
            data = data.strip()
        self.item = self.push_data(self.item, self.comment_key, data)

    def push_data(self, item, key, data):
        if self.postprocessor is not None:
            result = self.postprocessor(self.path, key, data)
            if result is None:
                return item
            key, data = result
        return self._push_data(item, key, data)

    def _push_data(self, item, key, data):
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
          namespace_separator=':', disable_entities=True, process_comments=False, **kwargs):
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

    If `preserve_order` is `True`, every element that has any content keeps an
    additional `'#events'` entry: a list of `('text', data)` and
    `('element', name, value)` pairs recording the exact interleaving of text
    and child elements as they appeared in the document. This is useful for
    mixed content such as ``<p>Keep <b>24</b> hours <b>or more</b></p>``, whose
    text/element order the plain representation cannot reproduce. The rest of
    the dictionary keeps its usual shape, and `item_callback` receives the
    same representation when streaming. Pass the same flag to `unparse` to
    emit children in the recorded order instead of grouping them by key::

        >>> xml = '<p>Keep <b>24</b> hours <b>or more</b></p>'
        >>> doc = xmltodict.parse(xml, preserve_order=True)
        >>> doc['p']['#events']
        [('text', 'Keep '), ('element', 'b', {'#events': [('text', '24')]}), ('text', ' hours '), ('element', 'b', {'#events': [('text', 'or more')]})]
        >>> doc['p']['b']
        [{'#events': [('text', '24')]}, {'#events': [('text', 'or more')]}]
        >>> xmltodict.unparse(doc, preserve_order=True)
        '<?xml version="1.0" encoding="utf-8"?>\\n<p>Keep <b>24</b> hours <b>or more</b></p>'
    """
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
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
    parser.ordered_attributes = True
    parser.StartNamespaceDeclHandler = handler.startNamespaceDecl
    parser.StartElementHandler = handler.startElement
    parser.EndElementHandler = handler.endElement
    parser.CharacterDataHandler = handler.characters
    if process_comments:
        parser.CommentHandler = handler.comments
    parser.buffer_text = True
    if disable_entities:
        def _forbid_entities(*_args, **_kwargs):
            raise ValueError("entities are disabled")

        parser.EntityDeclHandler = _forbid_entities
    if hasattr(xml_input, 'read'):
        parser.ParseFile(xml_input)
    elif isgenerator(xml_input):
        for chunk in xml_input:
            parser.Parse(chunk, False)
        parser.Parse(b'', True)
    else:
        parser.Parse(xml_input, True)
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


def _emit_ordered(key, value, content_handler,
                  attr_prefix, cdata_key, depth, preprocessor,
                  pretty, newl, indent, namespace_separator,
                  namespaces, expand_iter,
                  encoding, bytes_errors, comment_key):
    """Emit one element from its ordered '#events' stream."""
    if isinstance(indent, int):
        indent = ' ' * indent
    attrs = {}
    for attr_key, attr_value in value.items():
        if not (isinstance(attr_key, str)
                and attr_key.startswith(attr_prefix)):
            continue
        attr_key = _process_namespace(attr_key, namespaces,
                                      namespace_separator, attr_prefix)
        if attr_key == attr_prefix + 'xmlns' and isinstance(attr_value, dict):
            for prefix, uri in attr_value.items():
                _validate_name(prefix, "attribute")
                attr = 'xmlns{}'.format(f':{prefix}' if prefix else '')
                attrs[attr] = '' if uri is None else _convert_value_to_string(
                    uri, encoding=encoding, bytes_errors=bytes_errors
                )
            continue
        if attr_value is None:
            attr_value = ''
        elif not isinstance(attr_value, str):
            attr_value = _convert_value_to_string(
                attr_value, encoding=encoding, bytes_errors=bytes_errors)
        attr_name = attr_key[len(attr_prefix):]
        _validate_name(attr_name, "attribute")
        attrs[attr_name] = attr_value
    emitted_key = _process_namespace(key, namespaces, namespace_separator,
                                     attr_prefix)
    if preprocessor is not None:
        result = preprocessor(emitted_key, value)
        if result is None:
            return
        emitted_key, replacement = result
        if replacement is not value:
            if isinstance(replacement, dict) and '#events' in replacement:
                value = replacement
            else:
                # The preprocessor produced a plain value; fall back to the
                # regular emission rules for it.
                _emit(emitted_key, replacement, content_handler,
                      attr_prefix, cdata_key, depth, preprocessor,
                      pretty, newl, indent, namespaces=namespaces,
                      namespace_separator=namespace_separator,
                      expand_iter=expand_iter, encoding=encoding,
                      bytes_errors=bytes_errors, comment_key=comment_key)
                return
    _validate_name(emitted_key, "element")
    if pretty:
        content_handler.ignorableWhitespace(depth * indent)
    content_handler.startElement(emitted_key, AttributesImpl(attrs))
    has_events = bool(value['#events'])
    for event in value['#events']:
        if pretty and event[0] == 'element':
            content_handler.ignorableWhitespace(newl)
        if event[0] == 'text':
            content_handler.characters(
                _convert_value_to_string(event[1], encoding=encoding,
                                         bytes_errors=bytes_errors))
        else:
            _emit(event[1], event[2], content_handler,
                  attr_prefix, cdata_key, depth + 1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key,
                  preserve_order=True)
    if pretty and has_events:
        content_handler.ignorableWhitespace(depth * indent)
    content_handler.endElement(emitted_key)
    if pretty and depth:
        content_handler.ignorableWhitespace(newl)


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
          comment_key='#comment',
          preserve_order=False):
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

    if preserve_order and isinstance(value, dict) and '#events' in value:
        _emit_ordered(key, value, content_handler,
                      attr_prefix, cdata_key, depth, preprocessor,
                      pretty, newl, indent, namespace_separator,
                      namespaces, expand_iter,
                      encoding, bytes_errors, comment_key)
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

    If `preserve_order` is `True`, dictionaries produced by
    `parse(..., preserve_order=True)` are emitted following their `'#events'`
    streams, keeping the original interleaving of text and child elements.
    Dictionaries without an `'#events'` entry are emitted exactly as usual.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    preserve_order = kwargs.pop('preserve_order', False)
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
            preserve_order=preserve_order,
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
