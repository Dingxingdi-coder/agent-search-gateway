from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.protocol import NDJSONDecoder, encode_request, encode_response


def test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests() -> None:
    decoder = NDJSONDecoder()
    assert decoder.feed(b'{"type":"keyword_search","query":"hel') == []
    decoded = decoder.feed(
        b'lo"}\n{"type":"llm_search","prompt":"find"}\n'
        b'{"type":"url_fetch","url":"https://example.com","focus":null}\n'
        b'{"type":"shutdown"}\n'
    )
    assert decoded == [
        KeywordSearchRequest("hello"),
        LLMSearchRequest("find"),
        URLFetchRequest("https://example.com", None),
        ShutdownRequest(),
    ]

    errors = decoder.feed(
        b"not-json\n"
        b'{"type":"unknown"}\n'
        b'{"type":"keyword_search"}\n'
        b'{"type":"url_fetch","url":1,"focus":null}\n'
        b'{"type":"shutdown","extra":true}\n'
    )
    assert len(errors) == 5
    assert all(isinstance(item, ErrorResponse) for item in errors)
    assert all(
        item.error is ErrorCode.BAD_REQUEST for item in errors if isinstance(item, ErrorResponse)
    )

    success = encode_response(SuccessResponse("done"))
    error = encode_response(ErrorResponse(ErrorCode.BAD_REQUEST, "bad"))
    assert success == b'{"ok":true,"text":"done"}\n'
    assert error == b'{"ok":false,"error":"bad_request","message":"bad"}\n'
    assert encode_request(ShutdownRequest()) == b'{"type":"shutdown"}\n'
    assert success.count(b"\n") == 1
