Add rate limiting to the API: 100 requests per minute.

Return a 429 when exceeded. It should be fair across users and not break the
existing integrations.
