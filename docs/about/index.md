# About

About Alcazaba: what guides its design, its license, and where its name comes from.

## Design

- **Secure defaults:** anything that could serve one user's data to another, trust data read
  from the backend, or let a client get around a protection is opt-in, and documented.
- **One package, a folder per tool:** each tool in `fastapi_alcazaba.<tool>`, its main class
  importable from `fastapi_alcazaba`. The tools share a backend, a circuit breaker, the metrics
  and the error format, instead of each bringing its own.
- **Optional dependencies:** FastAPI, Pydantic and typing-extensions are all Alcazaba
  requires. Each backend or integration is an extra, and the code that needs it imports it only
  when it's used.
- **Every change tested:** a bug fix comes with a test that fails without it, and the
  documentation's examples are apps that the tests run.
- **A careful supply chain:** new dependencies only when they're worth it, and never a release
  younger than three days.

## License and credits

--8<-- "README.md:license-and-credits"
