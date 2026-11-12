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


def _coerce_path(path):
    """Normalize a user-supplied path into a tuple of element-name strings.

    Accepts any iterable of strings or ``(name, attrs)`` pairs, mirroring the
    ``path`` tuples handed to ``item_callback``/``postprocessor``.
    """
    if isinstance(path, str):
        return (path,)
    return tuple(entry[0] if isinstance(entry, (tuple, list)) else entry
                 for entry in path)


def _resolve_path_rules(rules):
    """Turn path-based force_list rules into ``(mode, path_tuple)`` entries.

    Each rule is either an ancestor path (tuple/list/string of element names)
    whose matching elements are forced into lists, or a ``(path, enabled)``
    pair that explicitly enables (``True``) or disables (``False``) list
    forcing. More specific (longer) rules take precedence, so a disabling
    rule overrides an enabling rule inherited from a shorter ancestor path.
    """
    resolved = []
    if isinstance(rules, str):
        rules = (rules,)
    for rule in rules:
        if isinstance(rule, tuple) and len(rule) == 2 \
                and isinstance(rule[1], bool):
            path, enabled = rule
        else:
            path, enabled = rule, True
        resolved.append((bool(enabled), _coerce_path(path)))
    # Longest paths first so the most specific rule wins.
    resolved.sort(key=lambda entry: len(entry[1]), reverse=True)
    return resolved


def _path_rule_matches(rule_path, element_path):
    """Match a rule path against an element path.

    A rule matches the element at the rule path itself (matched as a suffix,
    so the document root does not have to be repeated) and every element
    sitting directly below that ancestor. This lets one rule such as
    ``('...', 'record')`` cover both the repeated ``record`` element and its
    child fields, including the truncated item itself in streaming mode.
    """
    rule_len = len(rule_path)
    if rule_len == 0:
        return True
    if rule_len > len(element_path):
        return False
    if element_path[-rule_len:] == rule_path:
        return True
    # Ancestor rule applies to its immediate children (and to the truncated
    # streaming item, which is exactly one level below it). Match the parent
    # chain as a suffix too, so an omitted document root still works.
    return (len(element_path) > rule_len
            and element_path[-rule_len - 1:-1] == rule_path)


def _matches_path_rules(path_rules, element_path):
    """Apply resolved path rules; most specific (longest) match wins.

    Returns ``None`` when no rule matches, otherwise the enabling flag of the
    winning rule.
    """
    for enabled, rule_path in path_rules:
        if _path_rule_matches(rule_path, element_path):
            return enabled
    return None


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
        force_list_paths=None,
        comment_key="#comment",
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
        self.force_list_paths = (
            _resolve_path_rules(force_list_paths)
            if force_list_paths else None
        )
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

    def endElement(self, full_name):
        name = self._build_name(full_name)
        # If we just closed an item at the streaming depth, emit it and drop it
        # without attaching it back to its parent. This avoids accumulating all
        # streamed items in memory when using item_depth > 0.
        if len(self.path) == self.item_depth:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            if self.strip_whitespace and data:
                data = data.strip() or None
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
                self.item, self.data = self.stack.pop()
            else:
                self.item = None
                self.data = []
            self.path.pop()
            return
        if self.stack:
            data = (None if not self.data
                    else self.cdata_separator.join(self.data))
            item = self.item
            self.item, self.data = self.stack.pop()
            if self.strip_whitespace and data:
                data = data.strip() or None
            if data and self._should_force_cdata(name, data) and item is None:
                item = self.dict_constructor()
            if item is not None:
                if data:
                    self.push_data(item, self.cdata_key, data)
                self.item = self.push_data(self.item, name, item)
            else:
                self.item = self.push_data(self.item, name, data)
        else:
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
        if self.force_list_paths is not None:
            element_path = _coerce_path(self.path)
            verdict = _matches_path_rules(self.force_list_paths, element_path)
            if verdict is not None:
                return verdict
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
          force_list_paths=None, **kwargs):
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

        `force_list_paths` targets elements by their location instead of just
        their tag name. It is an iterable of rules, where each rule is an
        ancestor path (a tuple/list of element names, or a single string)
        expressed with the same name semantics as the parsed dictionary:
        namespace-prefixed names such as `'http://a.com/:name'` or
        `'ns_a:name'` are matched exactly, so two elements sharing the local
        name `name` in different namespaces can be told apart. A rule matches
        any element whose path ends with that path (the full document root
        does not have to be repeated) or that sits directly below a named
        ancestor path. Longer, more specific rules inherit and override
        shorter ones; a `(path, False)` pair explicitly disables list forcing
        at that location, overriding an enabling ancestor rule. When streaming
        with `item_depth`, the element path seen here is the same truncated
        path that `item_callback` receives. Rules are consulted first;
        locations they do not mention still fall back to `force_list`.

        For example, given a document where `rec:name` repeats inside records
        but the metadata `meta:name` is always single-valued::

            force_list_paths=[
                ('http://supplier/record:record',),      # records and their fields
                (('http://supplier/record:record',
                  'http://supplier/meta:name'), False),  # ... except metadata name
            ]


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
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              force_list_paths=force_list_paths,
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


def _should_unparse_as_list(unparse_list_keys, path, key, value):
    """Decide whether a dict value must be emitted as repeated child elements.

    Path-based rules are resolved first (deepest match wins, with explicit
    disabling overriding an inherited enabling rule). Otherwise the legacy
    set of key names applies. Returns ``None`` when no rule mentions the
    location, leaving the natural shape of `value` untouched.
    """
    if unparse_list_keys is None:
        return None
    element_path = tuple(_coerce_path(path + (key,)))
    rules, plain_keys = unparse_list_keys
    if rules is not None:
        verdict = _matches_path_rules(rules, element_path)
        if verdict is not None:
            return verdict
    if plain_keys is not None and key in plain_keys:
        return True
    # No rule mentions this location: leave the natural shape of the value
    # alone instead of implicitly treating it as a non-list.
    return None


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
          comment_key='#comment',
          unparse_list_keys=None,
          path=()):
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

    path_key = key
    key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    _validate_name(key, "element")
    # Path rules use the dictionary's own key names, matching the same
    # namespace semantics as parse (full URIs when no mapping is supplied).
    list_verdict = _should_unparse_as_list(
        unparse_list_keys, path, path_key, value)
    if list_verdict is True and not isinstance(value, list):
        value = [value]
    elif list_verdict is False and isinstance(value, list) and len(value) == 1:
        value = value[0]
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
                  bytes_errors=bytes_errors, comment_key=comment_key,
                  unparse_list_keys=unparse_list_keys,
                  path=path + (key,))
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
            unparse_list_keys=None, **kwargs):
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

    `unparse_list_keys` is the write-back side of path-aware list forcing. It
    accepts an iterable of key-name strings (like `parse`'s `force_list`
    tuple) and/or the same path rules described for `force_list_paths`.
    Matching locations are always emitted as repeated elements, so a single
    dict is wrapped as one element. A ``(path, False)`` rule marks a location
    that is never a list: a one-item list there is unwrapped and emitted as a
    plain element, which avoids accidentally adding a duplicate element for a
    single-valued field. Without this option `unparse` keeps its previous
    shape-driven behavior.

    """
    bytes_errors = kwargs.pop('bytes_errors', 'replace')
    try:
        codecs.lookup_error(bytes_errors)
    except LookupError as exc:
        raise ValueError(f"Invalid bytes_errors handler: {bytes_errors}") from exc

    if unparse_list_keys is not None:
        if isinstance(unparse_list_keys, str):
            unparse_list_keys = (unparse_list_keys,)
        plain_keys = set()
        path_rules = []
        for entry in unparse_list_keys:
            if isinstance(entry, str):
                plain_keys.add(entry)
            else:
                path_rules.append(entry)
        unparse_list_keys = (
            _resolve_path_rules(path_rules) if path_rules else None,
            plain_keys or None,
        )

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
            unparse_list_keys=unparse_list_keys,
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
