from bot.intake import decode, join_chunks


def test_join_chunks_split_mid_sentence_uses_space():
    assert join_chunks(["The keeper moved", "too late."]) == "The keeper moved too late."


def test_join_chunks_split_at_sentence_end_uses_newline():
    assert join_chunks(["First paragraph.", "Second paragraph."]) == "First paragraph.\nSecond paragraph."


def test_decode_handles_bom_and_windows_encoding():
    assert decode("﻿Güler".encode("utf-8")) == "Güler"
    assert decode("Güler".encode("cp1252")) == "Güler"
