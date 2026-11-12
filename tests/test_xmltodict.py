from xmltodict import (
    ParsingInterrupted,
    XMLPostprocessorError,
    XMLSyntaxError,
    parse,
)
import collections
import pytest
from io import BytesIO

from xml.parsers.expat import ParserCreate
from xml.parsers import expat


def test_string_vs_file():
    xml = '<a>data</a>'
    assert parse(xml) == parse(BytesIO(xml.encode('ascii')))


def test_minimal():
    assert parse('<a/>') == {'a': None}
    assert parse('<a/>', force_cdata=True) == {'a': None}


def test_simple():
    assert parse('<a>data</a>') == {'a': 'data'}


def test_force_cdata():
    assert parse('<a>data</a>', force_cdata=True) == {'a': {'#text': 'data'}}


def test_selective_force_cdata_tuple():
    xml = "<a><b>data1</b><c>data2</c><d>data3</d></a>"
    # Test with tuple of specific element names
    result = parse(xml, force_cdata=("b", "d"))
    expected = {
        "a": {"b": {"#text": "data1"}, "c": "data2", "d": {"#text": "data3"}}
    }
    assert result == expected


def test_selective_force_cdata_single_element():
    xml = "<a><b>data1</b><c>data2</c></a>"
    # Test with single element name
    result = parse(xml, force_cdata=("b",))
    expected = {"a": {"b": {"#text": "data1"}, "c": "data2"}}
    assert result == expected


def test_selective_force_cdata_empty_tuple():
    xml = "<a><b>data1</b><c>data2</c></a>"
    # Test with empty tuple (should behave like force_cdata=False)
    result = parse(xml, force_cdata=())
    expected = {"a": {"b": "data1", "c": "data2"}}
    assert result == expected


def test_selective_force_cdata_callable():
    xml = "<a><b>data1</b><c>data2</c><d>data3</d></a>"

    # Test with callable function
    def should_force_cdata(path, key, value):
        return key in ["b", "d"]

    result = parse(xml, force_cdata=should_force_cdata)
    expected = {
        "a": {"b": {"#text": "data1"}, "c": "data2", "d": {"#text": "data3"}}
    }
    assert result == expected


def test_selective_force_cdata_nested_elements():
    xml = "<a><b><c>data1</c></b><d>data2</d></a>"
    # Test with nested elements - only 'c' should be forced
    result = parse(xml, force_cdata=("c",))
    expected = {"a": {"b": {"c": {"#text": "data1"}}, "d": "data2"}}
    assert result == expected


def test_selective_force_cdata_with_attributes():
    xml = '<a><b attr="value">data1</b><c>data2</c></a>'
    # Test with attributes - force_cdata should still work
    result = parse(xml, force_cdata=("b",))
    expected = {"a": {"b": {"@attr": "value", "#text": "data1"}, "c": "data2"}}
    assert result == expected


def test_selective_force_cdata_backwards_compatibility():
    xml = "<a><b>data1</b><c>data2</c></a>"
    # Test that boolean True still works (backwards compatibility)
    result_true = parse(xml, force_cdata=True)
    expected_true = {"a": {"b": {"#text": "data1"}, "c": {"#text": "data2"}}}
    assert result_true == expected_true

    # Test that boolean False still works (backwards compatibility)
    result_false = parse(xml, force_cdata=False)
    expected_false = {"a": {"b": "data1", "c": "data2"}}
    assert result_false == expected_false


def test_custom_cdata():
    assert parse('<a>data</a>', force_cdata=True, cdata_key='_CDATA_') == {'a': {'_CDATA_': 'data'}}


def test_list():
    assert parse('<a><b>1</b><b>2</b><b>3</b></a>') == {'a': {'b': ['1', '2', '3']}}


def test_attrib():
    assert parse('<a href="xyz"/>') == {'a': {'@href': 'xyz'}}


def test_skip_attrib():
    assert parse('<a href="xyz"/>', xml_attribs=False) == {'a': None}


def test_custom_attrib():
    assert parse('<a href="xyz"/>', attr_prefix='!') == {'a': {'!href': 'xyz'}}


def test_attrib_and_cdata():
    assert parse('<a href="xyz">123</a>') == {'a': {'@href': 'xyz', '#text': '123'}}


def test_semi_structured():
    assert parse('<a>abc<b/>def</a>') == {'a': {'b': None, '#text': 'abcdef'}}
    assert parse('<a>abc<b/>def</a>', cdata_separator='\n') == {'a': {'b': None, '#text': 'abc\ndef'}}


def test_nested_semi_structured():
    assert parse('<a>abc<b>123<c/>456</b>def</a>') == {'a': {'#text': 'abcdef', 'b': {'#text': '123456', 'c': None}}}


def test_skip_whitespace():
    xml = """
    <root>


      <emptya>           </emptya>
      <emptyb attr="attrvalue">


      </emptyb>
      <value>hello</value>
    </root>
    """
    assert parse(xml) == {'root': {'emptya': None, 'emptyb': {'@attr': 'attrvalue'}, 'value': 'hello'}}


def test_keep_whitespace():
    xml = "<root> </root>"
    assert parse(xml) == dict(root=None)
    assert parse(xml, strip_whitespace=False) == dict(root=' ')


def test_streaming():
    def cb(path, item):
        cb.count += 1
        assert path == [('a', {'x': 'y'}), ('b', None)]
        assert item == str(cb.count)
        return True
    cb.count = 0
    parse('<a x="y"><b>1</b><b>2</b><b>3</b></a>', item_depth=2, item_callback=cb)
    assert cb.count == 3


def test_streaming_interrupt():
    def cb(path, item):
        return False
    with pytest.raises(ParsingInterrupted):
        parse('<a>x</a>', item_depth=1, item_callback=cb)


def test_streaming_generator():
    def cb(path, item):
        cb.count += 1
        assert path == [('a', {'x': 'y'}), ('b', None)]
        assert item == str(cb.count)
        return True
    cb.count = 0
    parse((n for n in '<a x="y"><b>1</b><b>2</b><b>3</b></a>'), item_depth=2, item_callback=cb)
    assert cb.count == 3


def test_streaming_returns_none():
    # When streaming (item_depth > 0), parse should return None
    def cb(path, item):
        return True

    result = parse("<a><b>1</b><b>2</b></a>", item_depth=2, item_callback=cb)
    assert result is None


def _stream(xml, depth, **kwargs):
    items = []
    parse(xml, item_depth=depth,
          item_callback=lambda path, item: items.append(item) or True,
          **kwargs)
    return items


def test_streaming_text_with_attrs():
    # char data was dropped when the streamed item had attributes (#257)
    xml = '<items><item id="1">hello</item></items>'
    assert _stream(xml, 2) == [{'@id': '1', '#text': 'hello'}]


def test_streaming_text_postprocessor():
    def postprocessor(path, key, value):
        if key == '#text':
            assert path == [('items', None), ('item', {'id': '1'})]
            return 'text', value.upper()
        return key, value

    xml = '<items><item id="1">hello</item></items>'
    assert _stream(xml, 2, postprocessor=postprocessor) == \
        [{'@id': '1', 'text': 'HELLO'}]


def test_streaming_text_with_children():
    xml = '<a>free text<b>1</b><b>2</b></a>'
    assert _stream(xml, 1) == [{'b': ['1', '2'], '#text': 'free text'}]
    xml = '<items><item><sub>x</sub>tail</item></items>'
    assert _stream(xml, 2) == [{'sub': 'x', '#text': 'tail'}]


def test_streaming_text_whitespace():
    xml = '<items><item id="1"> spaced </item></items>'
    assert _stream(xml, 2) == [{'@id': '1', '#text': 'spaced'}]
    assert _stream(xml, 2, strip_whitespace=False) == \
        [{'@id': '1', '#text': ' spaced '}]
    # whitespace-only text is stripped to nothing, like non-streaming parse
    xml = '<items><item id="1"> </item></items>'
    assert _stream(xml, 2) == [{'@id': '1'}]


def test_streaming_force_cdata_and_cdata_key():
    xml = '<items><item>x</item></items>'
    assert _stream(xml, 2, force_cdata=True) == [{'#text': 'x'}]
    xml = '<items><item id="1">x</item></items>'
    assert _stream(xml, 2, cdata_key='$') == [{'@id': '1', '$': 'x'}]


def test_streaming_matches_nonstreaming():
    docs = [
        '<r><item>plain</item></r>',
        '<r><item id="1">text</item><item>only</item></r>',
        '<r><item a="1"><c>x</c>tail</item><item a="2">lead<c>y</c></item></r>',
        '<r><item><sub><deep d="3">v</deep></sub>mixed</item></r>',
        '<r><item> </item><item id="1"> </item></r>',
    ]
    for xml in docs:
        expected = parse(xml)['r']['item']
        if not isinstance(expected, list):
            expected = [expected]
        assert _stream(xml, 2) == expected, xml


def test_streaming_random_docs_match_nonstreaming():
    import random
    rnd = random.Random(20260715)
    for _ in range(200):
        items = []
        for i in range(rnd.randint(1, 4)):
            attr = f' id="{i}"' if rnd.random() < 0.5 else ''
            children = ''.join(f'<c{j}>v{j}</c{j}>'
                               for j in range(rnd.randint(0, 2)))
            text = rnd.choice(['', ' ', 'txt', 'a b'])
            if rnd.random() < 0.5:
                body = text + children
            else:
                body = children + text
            items.append(f'<item{attr}>{body}</item>')
        xml = f"<r>{''.join(items)}</r>"
        expected = parse(xml)['r']['item']
        if not isinstance(expected, list):
            expected = [expected]
        assert _stream(xml, 2) == expected, xml


def test_postprocessor():
    def postprocessor(path, key, value):
        try:
            return key + ':int', int(value)
        except (ValueError, TypeError):
            return key, value
    assert {'a': {'b:int': [1, 2], 'b': 'x'}} == parse('<a><b>1</b><b>2</b><b>x</b></a>', postprocessor=postprocessor)


def test_postprocessor_attribute():
    def postprocessor(path, key, value):
        try:
            return key + ':int', int(value)
        except (ValueError, TypeError):
            return key, value
    assert {'a': {'@b:int': 1}} == parse('<a b="1"/>', postprocessor=postprocessor)


def test_postprocessor_skip():
    def postprocessor(path, key, value):
        if key == 'b':
            value = int(value)
            if value == 3:
                return None
        return key, value
    assert {'a': {'b': [1, 2]}} == parse('<a><b>1</b><b>2</b><b>3</b></a>', postprocessor=postprocessor)


def test_unicode():
    value = chr(39321)
    assert {'a': value} == parse(f'<a>{value}</a>')


def test_encoded_string():
    value = chr(39321)
    xml = f'<a>{value}</a>'
    assert parse(xml) == parse(xml.encode('utf-8'))


def test_namespace_support():
    xml = """
    <root xmlns="http://defaultns.com/"
          xmlns:a="http://a.com/"
          xmlns:b="http://b.com/"
          version="1.00">
      <x a:attr="val">1</x>
      <a:y>2</a:y>
      <b:z>3</b:z>
    </root>
    """
    d = {
        'http://defaultns.com/:root': {
            '@version': '1.00',
            '@xmlns': {
                '': 'http://defaultns.com/',
                'a': 'http://a.com/',
                'b': 'http://b.com/',
            },
            'http://defaultns.com/:x': {
                '@http://a.com/:attr': 'val',
                '#text': '1',
            },
            'http://a.com/:y': '2',
            'http://b.com/:z': '3',
        }
    }
    res = parse(xml, process_namespaces=True)
    assert res == d


def test_namespace_collapse():
    xml = """
    <root xmlns="http://defaultns.com/"
          xmlns:a="http://a.com/"
          xmlns:b="http://b.com/"
          version="1.00">
      <x a:attr="val">1</x>
      <a:y>2</a:y>
      <b:z>3</b:z>
    </root>
    """
    namespaces = {
        'http://defaultns.com/': '',
        'http://a.com/': 'ns_a',
    }
    d = {
        'root': {
            '@version': '1.00',
            '@xmlns': {
                '': 'http://defaultns.com/',
                'a': 'http://a.com/',
                'b': 'http://b.com/',
            },
            'x': {
                '@ns_a:attr': 'val',
                '#text': '1',
            },
            'ns_a:y': '2',
            'http://b.com/:z': '3',
        },
    }
    res = parse(xml, process_namespaces=True, namespaces=namespaces)
    assert res == d


def test_namespace_collapse_all():
    xml = """
    <root xmlns="http://defaultns.com/"
          xmlns:a="http://a.com/"
          xmlns:b="http://b.com/"
          version="1.00">
      <x a:attr="val">1</x>
      <a:y>2</a:y>
      <b:z>3</b:z>
    </root>
    """
    namespaces = collections.defaultdict(lambda: None)
    d = {
        'root': {
            '@version': '1.00',
            '@xmlns': {
                '': 'http://defaultns.com/',
                'a': 'http://a.com/',
                'b': 'http://b.com/',
            },
            'x': {
                '@attr': 'val',
                '#text': '1',
            },
            'y': '2',
            'z': '3',
        },
    }
    res = parse(xml, process_namespaces=True, namespaces=namespaces)
    assert res == d


def test_namespace_ignore():
    xml = """
    <root xmlns="http://defaultns.com/"
          xmlns:a="http://a.com/"
          xmlns:b="http://b.com/"
          version="1.00">
      <x>1</x>
      <a:y>2</a:y>
      <b:z>3</b:z>
    </root>
    """
    d = {
        'root': {
            '@xmlns': 'http://defaultns.com/',
            '@xmlns:a': 'http://a.com/',
            '@xmlns:b': 'http://b.com/',
            '@version': '1.00',
            'x': '1',
            'a:y': '2',
            'b:z': '3',
        },
    }
    assert parse(xml) == d


def test_force_list_basic():
    xml = """
    <servers>
      <server>
        <name>server1</name>
        <os>os1</os>
      </server>
    </servers>
    """
    expectedResult = {
        'servers': {
            'server': [
                {
                    'name': 'server1',
                    'os': 'os1',
                },
            ],
        }
    }
    assert parse(xml, force_list=('server',)) == expectedResult


def test_force_list_callable():
    xml = """
    <config>
        <servers>
          <server>
            <name>server1</name>
            <os>os1</os>
          </server>
        </servers>
        <skip>
            <server></server>
        </skip>
    </config>
    """

    def force_list(path, key, value):
        """Only return True for servers/server, but not for skip/server."""
        if key != 'server':
            return False
        return path and path[-1][0] == 'servers'

    expectedResult = {
        'config': {
            'servers': {
                'server': [
                    {
                        'name': 'server1',
                        'os': 'os1',
                    },
                ],
            },
            'skip': {
                'server': None,
            },
        },
    }
    assert parse(xml, force_list=force_list, dict_constructor=dict) == expectedResult


def test_disable_entities_true_rejects_xmlbomb():
    xml = """
    <!DOCTYPE xmlbomb [
        <!ENTITY a "1234567890" >
        <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">
        <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">
    ]>
    <bomb>&c;</bomb>
    """
    with pytest.raises(ValueError, match="entities are disabled"):
        parse(xml, disable_entities=True)


def test_disable_entities_false_returns_xmlbomb():
    xml = """
    <!DOCTYPE xmlbomb [
        <!ENTITY a "1234567890" >
        <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">
        <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">
    ]>
    <bomb>&c;</bomb>
    """
    bomb = "1234567890" * 64
    expectedResult = {'bomb': bomb}
    assert parse(xml, disable_entities=False) == expectedResult


def test_external_entity():
    xml = """
    <!DOCTYPE external [
        <!ENTITY ee SYSTEM "http://www.python.org/">
    ]>
    <root>&ee;</root>
    """
    with pytest.raises(ValueError, match="entities are disabled"):
        parse(xml)
    assert parse(xml, disable_entities=False) == {"root": None}


def test_external_entity_with_custom_expat():
    xml = """
    <!DOCTYPE external [
        <!ENTITY ee SYSTEM "http://www.python.org/">
    ]>
    <root>&ee;</root>
    """

    class CustomExpat:
        def __init__(self, external_entity_result):
            self.external_entity_result = external_entity_result

        def ParserCreate(self, *args, **kwargs):
            parser = ParserCreate(*args, **kwargs)

            def _handler(*args, **kwargs):
                return self.external_entity_result

            parser.ExternalEntityRefHandler = _handler
            return parser

        ExpatError = expat.ExpatError

    with pytest.raises(expat.ExpatError):
        parse(xml, disable_entities=False, expat=CustomExpat(0))
    assert parse(xml, disable_entities=False, expat=CustomExpat(1)) == {"root": None}
    with pytest.raises(ValueError):
        assert parse(xml, disable_entities=True, expat=CustomExpat(1))
    with pytest.raises(ValueError):
        assert parse(xml, disable_entities=True, expat=CustomExpat(0))


def test_disable_entities_true_allows_doctype_without_entities():
    xml = """<?xml version='1.0' encoding='UTF-8'?>
    <!DOCTYPE data SYSTEM "diagram.dtd">
    <foo>bar</foo>
    """
    assert parse(xml, disable_entities=True) == {"foo": "bar"}
    assert parse(xml, disable_entities=False) == {"foo": "bar"}


def test_disable_entities_allows_comments_by_default():
    xml = """
    <a>
        <!-- ignored -->
        <b>1</b>
    </a>
    """
    assert parse(xml) == {'a': {'b': '1'}}


def test_comments():
    xml = """
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
    """
    expectedResult = {
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
    }
    assert parse(xml, process_comments=True) == expectedResult


def test_streaming_with_comments_and_attrs():
    xml = """
    <a>
        <b attr1="value">
            <!-- note -->
            <c>cdata</c>
        </b>
    </a>
    """

    def handler(path, item):
        expected = {
            "@attr1": "value",
            "#comment": "note",
            "c": "cdata",
        }
        assert expected == item
        return True

    parse(xml, item_depth=2, item_callback=handler, process_comments=True)


def test_streaming_memory_usage():
    # Guard against re-introducing accumulation of streamed items into parent
    try:
        import tracemalloc
    except ImportError:
        pytest.skip("tracemalloc not available")

    NUM_ITEMS = 20000

    def xml_gen():
        yield "<a>"
        # generate many children with attribute and text
        for i in range(NUM_ITEMS):
            yield f'<b attr="v">{i % 10}</b>'
        yield "</a>"

    count = 0

    def cb(path, item):
        nonlocal count
        count += 1
        return True

    tracemalloc.start()
    parse(xml_gen(), item_depth=2, item_callback=cb)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert count == NUM_ITEMS
    # Peak memory should remain reasonably bounded; choose a conservative threshold
    # This value should stay well below pathological accumulation levels
    MAX_BYTES = 32 * 1024  # 32 KiB
    assert peak < MAX_BYTES, f"peak memory too high: {peak} bytes"


def test_streaming_attrs():
    xml = """
    <a>
        <b attr1="value">
            <c>cdata</c>
        </b>
    </a>
    """
    def handler(path, item):
        expected = {
            '@attr1': 'value',
            'c': 'cdata'
        }
        assert expected == item
        return True

    parse(xml, item_depth=2, item_callback=handler)


def test_namespace_on_root_without_other_attrs():
    xml = """
    <MyXML xmlns="http://www.xml.org/schemas/Test">
        <Tag1>Text1</Tag1>
        <Tag2 attr2="en">Text2</Tag2>
        <Tag3>Text3</Tag3>
        <Tag4 attr4="en">Text4</Tag4>
    </MyXML>
    """
    namespaces = {
        "http://www.xml.org/schemas/Test": None,
    }
    expected = {
        "MyXML": {
            "@xmlns": {"": "http://www.xml.org/schemas/Test"},
            "Tag1": "Text1",
            "Tag2": {"@attr2": "en", "#text": "Text2"},
            "Tag3": "Text3",
            "Tag4": {"@attr4": "en", "#text": "Text4"},
        }
    }
    assert parse(xml, process_namespaces=True, namespaces=namespaces) == expected


def test_force_list_true_applies_to_all_keys():
    xml = "<a><b>1</b><b>2</b><c>x</c></a>"
    expected = {"a": [{"b": ["1", "2"], "c": ["x"]}]}
    assert parse(xml, force_list=True) == expected


CONTEXT_XML = (
    "<records>\n"
    "  <record><value>10</value><value>11</value></record>\n"
    "  <record><value>20</value><value>21</value></record>\n"
    "  <record><value>30</value><value>坏</value></record>\n"
    "</records>"
)


def _context_chunks(size):
    data = CONTEXT_XML.encode("utf-8")
    for start in range(0, len(data), size):
        yield data[start:start + size]


def _context_str_chunks(size):
    for start in range(0, len(CONTEXT_XML), size):
        yield CONTEXT_XML[start:start + size]


def _int_postprocessor(path, key, value):
    if key == "value":
        return key, int(value)
    return key, value


def test_postprocessor_error_context_whole_input():
    delivered = []

    def callback(path, item):
        delivered.append(item)
        return True

    with pytest.raises(XMLPostprocessorError) as exc_info:
        parse(CONTEXT_XML, item_depth=2, item_callback=callback,
              postprocessor=_int_postprocessor, error_context=True)

    error = exc_info.value
    assert isinstance(error.__cause__, ValueError)
    assert error.context == {
        "record_index": 3,
        "element_path": ("records", "record", "value"),
        "field_name": "value",
        "line": 4,
        "column": 35,
        "byte_index": 152,
        "position_kind": "value",
    }
    # The first two records were delivered before the third failed.
    assert delivered == [
        {"value": [10, 11]},
        {"value": [20, 21]},
    ]

    # The same position can be checked directly against the source bytes.
    source = CONTEXT_XML.encode("utf-8")
    assert source[error.byte_index:].startswith("坏".encode("utf-8"))


def test_postprocessor_error_context_chunked_input():
    for chunker in (_context_chunks, _context_str_chunks):
        for chunk_size in (7, 13):
            delivered = []

            def callback(path, item):
                delivered.append(item)
                return True

            with pytest.raises(XMLPostprocessorError) as exc_info:
                parse(chunker(chunk_size), item_depth=2,
                      item_callback=callback,
                      postprocessor=_int_postprocessor, error_context=True)

            error = exc_info.value
            assert error.record_index == 3
            assert error.element_path == ("records", "record", "value")
            assert error.field_name == "value"
            assert error.line == 4
            assert error.column == 35
            assert error.byte_index == 152
            assert error.position_kind == "value"
            assert delivered == [
                {"value": [10, 11]},
                {"value": [20, 21]},
            ]


def test_postprocessor_error_keeps_value_error_compatibility():
    with pytest.raises(ValueError) as exc_info:
        parse("<r><value>x</value></r>",
              postprocessor=_int_postprocessor, error_context=True)
    assert isinstance(exc_info.value, XMLPostprocessorError)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_error_context_aliases():
    for option in ("source_context", "include_error_context"):
        with pytest.raises(XMLPostprocessorError):
            parse("<r><value>x</value></r>",
                  postprocessor=_int_postprocessor, **{option: True})


def test_postprocessor_error_context_is_opt_in():
    def postprocessor(path, key, value):
        if key == "value":
            return key, int(value)
        return key, value

    with pytest.raises(ValueError) as exc_info:
        parse("<r><value>x</value></r>", postprocessor=postprocessor)

    assert not isinstance(exc_info.value, XMLPostprocessorError)


def test_postprocessor_attribute_error_context_points_to_attribute_value():
    def postprocessor(path, key, value):
        if key == "@code":
            raise ValueError("bad code")
        return key, value

    with pytest.raises(XMLPostprocessorError) as exc_info:
        parse('<r code="中x"/>', postprocessor=postprocessor,
              error_context=True)

    error = exc_info.value
    assert error.field_name == "@code"
    assert error.position_kind == "attribute_value"
    assert error.line == 1
    assert error.column == 10
    assert error.byte_index == 9


def test_postprocessor_element_error_context_points_to_start_tag():
    def postprocessor(path, key, value):
        if key == "bad" and isinstance(value, dict):
            raise ValueError("bad nested record")
        return key, value

    with pytest.raises(XMLPostprocessorError) as exc_info:
        parse("<r>\n  <bad><x>v</x></bad>\n</r>",
              postprocessor=postprocessor, error_context=True)

    error = exc_info.value
    assert error.position_kind == "start_tag"
    assert error.element_path == ("r", "bad")
    assert error.field_name == "bad"
    assert error.line == 2
    assert error.column == 3
    assert error.byte_index == 6


def test_parser_error_context_is_distinct_and_keeps_prior_callbacks():
    delivered = []

    def callback(path, item):
        delivered.append(item)
        return True

    with pytest.raises(XMLSyntaxError) as exc_info:
        parse(
            "<records><record><value>1</value></record>"
            "<record><value>2<</value></record></records>",
            item_depth=2, item_callback=callback, error_context=True,
        )

    error = exc_info.value
    assert isinstance(error.__cause__, expat.ExpatError)
    assert error.position_kind == "parser_error"
    assert error.record_index == 2
    assert delivered == [{"value": "1"}]


def test_error_context_streaming_does_not_store_byte_input():
    try:
        import tracemalloc
    except ImportError:
        pytest.skip("tracemalloc not available")

    num_items = 20000

    def xml_gen():
        yield b"<a>"
        for index in range(num_items):
            yield f'<b attr="v">{index % 10}</b>'.encode()
        yield b"</a>"

    count = 0

    def callback(path, item):
        nonlocal count
        count += 1
        return True

    tracemalloc.start()
    parse(xml_gen(), item_depth=2, item_callback=callback,
          error_context=True)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert count == num_items
    assert peak < 32 * 1024


def test_parser_error_context_is_opt_in():
    with pytest.raises(expat.ExpatError) as exc_info:
        parse("<a>")
    assert not isinstance(exc_info.value, XMLSyntaxError)


def test_syntax_error_remains_compatible_with_expat_error_type():
    with pytest.raises(expat.ExpatError):
        parse("<a>", error_context=True)
