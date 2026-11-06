"""New passwords and Pwned Passwords' answers, from clients and a third party: any text is a range
answer's count (0 or more) or a ValueError, and any password gets problems from `PROBLEMS`, never
an exception; a password with a lone surrogate is refused, and a blocklisted one is found
whatever its case. The input is decoded with surrogateescape, so that it holds lone surrogates."""

import asyncio
import sys
from contextlib import nullcontext

try:
    import atheris  # pylint: disable=import-error  # in ClusterFuzzLite's image
except ImportError:  # the unit tests run the target without it
    atheris = None

with atheris.instrument_imports(include=["fastapi_alcazaba"]) if atheris else nullcontext():
    from fastapi_alcazaba.auth.passwords import (
        PROBLEMS,
        FastAPIPasswords,
        PasswordBlocklist,
        _range_count,
    )

SUFFIX = "0" * 35


def fuzz_one_input(data: bytes) -> None:
    text = data.decode("utf-8", "surrogateescape")
    try:
        count = _range_count(text, SUFFIX)
    except ValueError:
        pass
    else:
        assert 0 <= count < 10**12, count

    password, _, user_input = text.partition("\n")
    # In another case when it's ASCII: casefold("I") isn't casefold("ı"), whose upper() it is.
    blocklist = PasswordBlocklist([password.upper() if password.isascii() else password])
    passwords = FastAPIPasswords(min_length=8, blocklist=blocklist)
    result = asyncio.run(passwords.evaluate(password, user_inputs=[user_input]))
    assert set(result.problems) <= set(PROBLEMS), result
    assert list(result.problems) == sorted(result.problems, key=PROBLEMS.index), result
    assert result.score is None
    is_text = not any("\ud800" <= character <= "\udfff" for character in password)
    assert ("invalid_characters" in result.problems) is not is_text, result
    assert ("blocklisted" in result.problems) is (len(blocklist) == 1), result


def main() -> None:
    atheris.Setup(sys.argv, fuzz_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
