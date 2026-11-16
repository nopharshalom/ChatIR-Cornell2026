"""Text stop handling shared by the local chat adapters."""


def stop_sequences(stop):
    if stop is None:
        return []
    sequences = [stop] if isinstance(stop, str) else list(stop)
    if any(not isinstance(s, str) or not s for s in sequences):
        raise ValueError('Stop sequences must be nonempty strings.')
    return sequences


def first_stop(text, sequences):
    offsets = [text.find(s) for s in sequences if s in text]
    return min(offsets) if offsets else None


def stopped_text(text, sequences):
    offset = first_stop(text, sequences)
    return text if offset is None else text[:offset]
