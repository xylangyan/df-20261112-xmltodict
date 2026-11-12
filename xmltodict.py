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


class _NamespaceScope(dict):
    """A prefix->URI namespace scope: the current bindings plus a change log.

    Behaves like a plain dict of the bindings in effect, while `declared`
    records the bindings that were introduced at the element that opened this
    scope (so they can be replayed when the subtree is serialized on its
    own). `undeclared` records prefixes that were explicitly unbound at that
    element (e.g. ``xmlns=""``), which XML 1.0 serializers cannot express and
    which therefore must not be re-declared.
    """

    def __init__(self, bindings=(), declared=(), undeclared=()):
        super().__init__(bindings)
        self.declared = dict(declared)
        self.undeclared = set(undeclared)

    def child(self, declarations):
        declared = {}
        undeclared = set()
        for prefix, uri in declarations.items():
            if uri is None:
                undeclared.add(prefix)
            else:
                declared[prefix] = uri
        child_scope = _NamespaceScope(self, declared, undeclared)
        child_scope.update(declared)
        for prefix in undeclared:
            child_scope.pop(prefix, None)
        return child_scope


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
        namespace_scope=False,
        namespace_scope_key=None,
        collect_expanded_names=False,
        process_namespaces=False,
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
        self.namespace_scope = namespace_scope
        if namespace_scope_key is None:
            namespace_scope_key = attr_prefix + 'xmlns:scope'
        self.namespace_scope_key = namespace_scope_key
        if namespace_scope_key.startswith(attr_prefix):
            self.namespace_scope_attr_key = namespace_scope_key[
                len(attr_prefix):]
        else:
            self.namespace_scope_attr_key = namespace_scope_key
        self.collect_expanded_names = collect_expanded_names
        self.process_namespaces = process_namespaces
        self.namespace_scopes = []
        self.expanded_names = []

    @property
    def current_namespace_scope(self):
        if self.namespace_scopes:
            return self.namespace_scopes[-1]
        return _NamespaceScope()

    def _push_namespace_scope(self):
        scope = self.current_namespace_scope.child(
            self.namespace_declarations)
        self.namespace_declarations = self.dict_constructor()
        self.namespace_scopes.append(scope)
        return scope

    def _scope_annotation(self, scope):
        if not self.namespace_scope:
            return None
        if not (scope or scope.declared or scope.undeclared):
            return None
        annotation = self.dict_constructor(scope)
        for prefix in scope.undeclared:
            annotation[prefix] = None
        return annotation

    def _pop_namespace_scope(self):
        self.namespace_scopes.pop()

    def _record_expanded_name(self, kind, expanded_name):
        if self.collect_expanded_names:
            self.expanded_names.append((kind, expanded_name))

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
        # With namespace processing enabled Expat already hands us expanded
        # names (URI + separator + localname), so `full_name` is exactly the
        # expanded name we want to record. Unprefixed attributes are reported
        # without the default namespace, which is also correct.
        self._record_expanded_name('element', full_name)
        namespace_declarations = self.namespace_declarations
        if self.process_namespaces and self.namespace_scope:
            scope = self._push_namespace_scope()
            name = self._build_name(full_name)
        else:
            scope = None
            name = self._build_name(full_name)
        attrs = self._attrs_to_dict(attrs)
        for key in attrs:
            if key == 'xmlns' or key.startswith('xmlns:'):
                continue
            self._record_expanded_name('attribute', self._build_name(key))
        if namespace_declarations:
            if not attrs:
                attrs = self.dict_constructor()
            attrs['xmlns'] = namespace_declarations
            self.namespace_declarations = self.dict_constructor()
        if scope is not None:
            scope_annotation = self._scope_annotation(scope)
            if scope_annotation is not None:
                if not attrs:
                    attrs = self.dict_constructor()
                attrs[self.namespace_scope_attr_key] = scope_annotation
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
            if self.namespace_scopes:
                self._pop_namespace_scope()
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
        if self.namespace_scopes:
            self._pop_namespace_scope()

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
          namespace_scope=False, namespace_scope_key=None,
          collect_expanded_names=False, **kwargs):
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

    With `process_namespaces=True`, `namespace_scope=True` additionally
    attaches a scope-preserving entry to every element that carries namespace
    context. The entry lives under `namespace_scope_key` (default
    `'@xmlns:scope'`) and maps each in-scope prefix to its URI at that
    element (the default namespace is the `''` key; explicitly undeclared
    prefixes such as ``xmlns=""`` map to `None`). This keeps prefix
    re-bindings local to their subtree, so sibling elements after a
    re-binding element keep their original namespace, and a subtree can be
    serialized on its own with `unparse(..., expand_namespaces=True)`.

    With `collect_expanded_names=True`, `parse` returns a
    ``(item, expanded_names)`` tuple instead of just the item, where
    `expanded_names` is a list of ``(kind, name)`` pairs: an
    ``('element', name)`` entry for every element, in document order,
    followed by ``('attribute', name)`` entries for that element's
    attributes (xmlns declarations are not reported). Names use expanded
    form (``URI:local``) with `process_namespaces=True`, subject to the
    `namespaces` collapse map, or raw lexical names without it.
    """
    handler = _DictSAXHandler(namespace_separator=namespace_separator,
                              process_namespaces=process_namespaces,
                              namespace_scope=namespace_scope,
                              namespace_scope_key=namespace_scope_key,
                              collect_expanded_names=collect_expanded_names,
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
    if collect_expanded_names:
        return handler.item, list(handler.expanded_names)
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


def _split_expanded_name(name, attr_prefix='@', namespace_separator=':'):
    """Split an element/attribute name into (uri, local).

    Attribute-prefixed keys are accepted and returned without their
    `attr_prefix`. Returns (None, local) for names that are not
    namespace-expanded.
    """
    if not isinstance(name, str):
        return None, name
    stripped = name
    if stripped.startswith(attr_prefix):
        stripped = stripped[len(attr_prefix):]
    if namespace_separator not in stripped:
        return None, stripped
    uri, local = stripped.rsplit(namespace_separator, 1)
    if not uri:
        return None, stripped
    return uri, local


def _resolve_lexical_name(name, prefix_scope, attr_prefix='@',
                          namespace_separator=':'):
    """Resolve a name that may use a prefix bound in the current scope.

    Returns (uri, local, prefix). Expanded names (``URI:local``) and
    prefixed names bound in `prefix_scope` are supported. Plain names
    (without the namespace separator) are never namespaced, even when a
    default namespace is in scope: plain attributes ignore the default
    namespace by spec, and a plain element name means "no namespace".
    """
    stripped = name[len(attr_prefix):] if isinstance(name, str) and \
        name.startswith(attr_prefix) else name
    if not isinstance(stripped, str) or \
            namespace_separator not in stripped:
        return None, stripped, None
    prefix, local = stripped.split(namespace_separator, 1)
    if not prefix:
        return None, local, None
    if prefix in prefix_scope:
        return prefix_scope[prefix], local, prefix
    # Not a bound lexical prefix. An expanded name can be told apart from a
    # bare lexical prefix because URIs contain '/' or additional separators.
    if '/' in stripped or stripped.count(namespace_separator) > 1:
        uri, expanded_local = _split_expanded_name(
            name, attr_prefix, namespace_separator)
        return uri, expanded_local, None
    # An unbound lexical prefix: leave the name alone.
    return None, stripped, None


def _prefix_for_uri(uri, prefix_scope, allow_default=True):
    """Find a prefix already bound to `uri` in the current scope.

    Attribute names cannot use the default namespace, so callers resolving
    attributes pass `allow_default=False`.
    """
    for prefix, bound_uri in prefix_scope.items():
        if bound_uri == uri and (prefix or allow_default):
            return prefix
    return None


def _select_prefix(uri, namespaces, used_prefixes, allow_default=False):
    """Choose an output prefix for `uri`, avoiding used prefixes.

    Element names may use the default namespace (empty prefix) when the
    `namespaces` map says so; attribute names always need a real prefix.
    Generated names are stable across a document because `used_prefixes`
    keeps every prefix taken so far.
    """
    preferred = namespaces.get(uri) if namespaces else None
    if preferred == '' and allow_default:
        return ''
    candidates = [preferred] if preferred else []
    candidates.extend(
        'ns{}'.format(index) for index in range(len(used_prefixes) + 1))
    for prefix in candidates:
        if prefix and prefix not in used_prefixes:
            used_prefixes.add(prefix)
            return prefix
    prefix = 'ns{}'.format(len(used_prefixes) + 1)
    used_prefixes.add(prefix)
    return prefix


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
          expand_namespaces=False,
          namespace_scope_key=None,
          prefix_scope=None,
          used_prefixes=None,
          expanded_names=None):
    if namespace_scope_key is None:
        namespace_scope_key = attr_prefix + 'xmlns:scope'
    elif not namespace_scope_key.startswith(attr_prefix):
        namespace_scope_key = attr_prefix + namespace_scope_key
    if prefix_scope is None:
        prefix_scope = {}
    if used_prefixes is None:
        used_prefixes = set()
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

    if not expand_namespaces:
        key = _process_namespace(key, namespaces, namespace_separator, attr_prefix)
    if preprocessor is not None:
        result = preprocessor(key, value)
        if result is None:
            return
        key, value = result
    # Minimal validation to avoid breaking out of tag context
    if not expand_namespaces:
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
        scanned_decls = {}
        scope_annotation = None
        for ik, iv in v.items():
            if isinstance(ik, str) and ik.startswith(attr_prefix):
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for prefix, uri in iv.items():
                        _validate_name(prefix, "attribute")
                        attr = 'xmlns{}'.format(f':{prefix}' if prefix else '')
                        scanned_decls[attr] = '' if uri is None else _convert_value_to_string(
                            uri, encoding=encoding, bytes_errors=bytes_errors)
                elif ik == namespace_scope_key and isinstance(iv, dict):
                    scope_annotation = iv
        current_scope = dict(prefix_scope)
        for attr, value in scanned_decls.items():
            prefix = attr[len('xmlns:'):] if attr.startswith('xmlns:') else ''
            if value:
                current_scope[prefix] = value
            else:
                current_scope.pop(prefix, None)
        annotation_decls = {}
        if scope_annotation:
            for prefix, uri in scope_annotation.items():
                _validate_name(prefix, "attribute")
                if uri is None:
                    if current_scope.get(prefix) is not None:
                        annotation_decls[
                            'xmlns' if not prefix else 'xmlns:' + prefix] = ''
                    current_scope.pop(prefix, None)
                else:
                    value = _convert_value_to_string(
                        uri, encoding=encoding, bytes_errors=bytes_errors)
                    if current_scope.get(prefix) != value:
                        annotation_decls[
                            'xmlns' if not prefix else 'xmlns:' + prefix] = value
                    current_scope[prefix] = value
        element_uri, element_local, lexical_prefix = _resolve_lexical_name(
            key, current_scope, attr_prefix, namespace_separator)
        if expanded_names is not None and isinstance(key, str):
            if expand_namespaces and element_uri:
                expanded_names.append(('element', element_uri +
                                       namespace_separator + element_local))
            else:
                expanded_names.append(('element', key))
        element_key = key
        element_prefix_used = lexical_prefix
        per_element_decls = {}
        decls = {}

        def ensure_decl(prefix, uri):
            if not prefix or not uri:
                return
            attr = 'xmlns:' + prefix
            if attr in decls or attr in per_element_decls:
                return
            if prefix_scope.get(prefix) == uri:
                return
            per_element_decls[attr] = uri
            current_scope[prefix] = uri
            used_prefixes.add(prefix)
        if expand_namespaces:
            if element_uri:
                preferred = namespaces.get(element_uri) if namespaces else None
                if preferred == '':
                    element_prefix_used = ''
                    element_key = element_local
                else:
                    if preferred:
                        element_prefix_used = preferred
                    elif element_prefix_used is None:
                        element_prefix_used = _prefix_for_uri(
                            element_uri, current_scope)
                    if element_prefix_used is None:
                        element_prefix_used = _select_prefix(
                            element_uri, None, used_prefixes,
                            allow_default=False)
                    if element_prefix_used:
                        element_key = element_prefix_used + \
                            namespace_separator + element_local
                    else:
                        element_key = element_local
        else:
            if element_prefix_used is None and element_uri and namespaces:
                preferred = namespaces.get(element_uri)
                if preferred:
                    element_prefix_used = preferred
            element_key = key
        if expand_namespaces and element_uri:
            if element_prefix_used:
                ensure_decl(element_prefix_used, element_uri)
            elif current_scope.get('') != element_uri:
                per_element_decls['xmlns'] = element_uri
                current_scope[''] = element_uri
        _validate_name(element_key, "element")
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
                if ik == namespace_scope_key and isinstance(iv, dict):
                    for attr_name, attr_value in annotation_decls.items():
                        decls[attr_name] = attr_value
                        attrs[attr_name] = attr_value
                    continue
                if ik == attr_prefix + 'xmlns' and isinstance(iv, dict):
                    for k, v in iv.items():
                        _validate_name(k, "attribute")
                        attr = 'xmlns{}'.format(f':{k}' if k else '')
                        value = '' if v is None else _convert_value_to_string(
                            v, encoding=encoding, bytes_errors=bytes_errors
                        )
                        decls[attr] = value
                        attrs[attr] = value
                    continue
                if expand_namespaces:
                    attr_uri, attr_local, attr_lexical = _resolve_lexical_name(
                        ik, current_scope, attr_prefix, namespace_separator)
                    if expanded_names is not None:
                        expanded_names.append((
                            'attribute',
                            attr_uri + namespace_separator + attr_local
                            if attr_uri else attr_local))
                    if attr_uri:
                        attr_prefix_used = attr_lexical
                        if attr_prefix_used is None:
                            attr_prefix_used = _prefix_for_uri(
                                attr_uri, current_scope,
                                allow_default=False)
                        if attr_prefix_used is None:
                            attr_prefix_used = _select_prefix(
                                attr_uri, None, used_prefixes)
                        ensure_decl(attr_prefix_used, attr_uri)
                        ik = attr_prefix + attr_prefix_used + \
                            namespace_separator + attr_local
                else:
                    if expanded_names is not None:
                        recorded = ik[len(attr_prefix):] if ik.startswith(
                            attr_prefix) else ik
                        expanded_names.append(('attribute', recorded))
                    ik = _process_namespace(ik, namespaces, namespace_separator,
                                            attr_prefix)
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
        if per_element_decls:
            for attr_name, attr_value in per_element_decls.items():
                attrs.setdefault(attr_name, attr_value)
        content_handler.startElement(element_key, AttributesImpl(attrs))
        if pretty and children:
            content_handler.ignorableWhitespace(newl)
        for child_key, child_value in children:
            _emit(child_key, child_value, content_handler,
                  attr_prefix, cdata_key, depth+1, preprocessor,
                  pretty, newl, indent, namespaces=namespaces,
                  namespace_separator=namespace_separator,
                  expand_iter=expand_iter, encoding=encoding,
                  bytes_errors=bytes_errors, comment_key=comment_key,
                  expand_namespaces=expand_namespaces,
                  namespace_scope_key=namespace_scope_key,
                  prefix_scope=current_scope,
                  used_prefixes=used_prefixes,
                  expanded_names=expanded_names)
        if cdata is not None:
            content_handler.characters(cdata)
        if pretty and children:
            content_handler.ignorableWhitespace(depth * indent)
        content_handler.endElement(element_key)
        if pretty and depth:
            content_handler.ignorableWhitespace(newl)


class _XMLGenerator(XMLGenerator):
    def comment(self, text):
        text = _validate_comment(text)
        self._write(f"<!--{escape(text)}-->")


def unparse(input_dict, output=None, encoding='utf-8', full_document=True,
            short_empty_elements=False, comment_key='#comment',
            expand_namespaces=False, namespace_scope_key=None,
            collect_expanded_names=False, **kwargs):
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

    With `expand_namespaces=True`, element and attribute names using expanded
    namespace names (``URI:local``) or scope-aware prefixes are serialized
    well-formed even when the input dictionary carries no matching ``@xmlns``
    declarations: the declarations they need are emitted automatically. This
    makes it safe to serialize a subtree extracted from a larger document.
    Prefixes already in scope (including those carried by the scope-preserving
    entry written by `parse(..., namespace_scope=True)`, keyed by
    `namespace_scope_key`, default `'@xmlns:scope'`) are reused, so explicit
    prefix re-bindings stay local to their subtree.

    With `collect_expanded_names=True`, `unparse` returns an
    ``(xml, expanded_names)`` tuple, where `expanded_names` is a list of
    ``(kind, name)`` pairs in document order: ``('element', name)`` for each
    element and ``('attribute', name)`` for each attribute, using expanded
    names from the input dictionary.
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
    namespaces = kwargs.get('namespaces')
    used_prefixes = set()
    if namespaces:
        used_prefixes = {
            prefix for prefix in namespaces.values() if prefix}
    expanded_names = [] if collect_expanded_names else None
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
            expand_namespaces=expand_namespaces,
            namespace_scope_key=namespace_scope_key,
            used_prefixes=used_prefixes,
            expanded_names=expanded_names,
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
        if collect_expanded_names:
            return value, expanded_names
        return value
    if collect_expanded_names:
        return expanded_names
    return None


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
