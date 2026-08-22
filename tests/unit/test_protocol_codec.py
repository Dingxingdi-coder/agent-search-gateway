from agent_search_gateway.errors import ErrorCode
from agent_search_gateway.models import (
    ErrorResponse,
    KeywordSearchRequest,
    LLMSearchRequest,
    PaperSearchRequest,
    ShutdownRequest,
    SuccessResponse,
    URLFetchRequest,
)
from agent_search_gateway.protocol import (
    _MAX_REQUEST_FRAME_BYTES,
    NDJSONDecoder,
    decode_request_frame,
    encode_request,
    encode_response,
)


def test_ndjson_codec_buffers_partial_bytes_and_splits_multiple_requests() -> None:
    decoder = NDJSONDecoder()
    assert decoder.feed(b'{"type":"keyword_search","query":"hel') == []
    decoded = decoder.feed(
        b'lo"}\n{"type":"llm_search","prompt":"find"}\n'
        b'{"type":"paper_search","query":"papers"}\n'
        b'{"type":"url_fetch","url":"https://example.com","focus":null}\n'
        b'{"type":"shutdown"}\n'
    )
    assert decoded == [
        KeywordSearchRequest("hello"),
        LLMSearchRequest("find"),
        PaperSearchRequest("papers"),
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
    assert encode_request(LLMSearchRequest("find")) == (
        b'{"type":"llm_search","prompt":"find"}\n'
    )
    assert encode_request(LLMSearchRequest("find", "paper")) == (
        b'{"type":"llm_search","prompt":"find","scope":"paper"}\n'
    )
    assert decode_request_frame(
        b'{"type":"llm_search","prompt":"find","scope":"all"}'
    ) == LLMSearchRequest("find", "all")
    assert decode_request_frame(
        b'{"type":"llm_search","prompt":"find","scope":"invalid"}'
    ) == ErrorResponse(ErrorCode.BAD_REQUEST, "Request fields do not match schema")
    assert encode_request(PaperSearchRequest("papers")) == (
        b'{"type":"paper_search","query":"papers"}\n'
    )
    assert encode_request(ShutdownRequest()) == b'{"type":"shutdown"}\n'
    assert decode_request_frame(b'{"type":"paper_search","query":"papers"}') == (
        PaperSearchRequest("papers")
    )
    assert decode_request_frame(b'{"type":"paper_search"}') == ErrorResponse(
        ErrorCode.BAD_REQUEST,
        "Request fields do not match schema",
    )
    assert decode_request_frame(
        b'{"type":"paper_search","query":"papers","extra":true}'
    ) == ErrorResponse(ErrorCode.BAD_REQUEST, "Request fields do not match schema")
    assert success.count(b"\n") == 1


def test_ndjson_decoder_rejects_oversized_frame_and_resynchronizes() -> None:
    decoder = NDJSONDecoder()
    oversized = decoder.feed(b"x" * (_MAX_REQUEST_FRAME_BYTES + 1))
    assert oversized == [ErrorResponse(ErrorCode.BAD_REQUEST, "Request frame is too large")]

    decoded = decoder.feed(b"discard-the-rest\n{\"type\":\"shutdown\"}\n")
    assert decoded == [ShutdownRequest()]
