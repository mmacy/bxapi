"""The adventure documents that come with the server.

Each `.json` file in this package is one adventure document. The server lists it with the id
`builtin-` followed by the file's name, like `builtin-barrow` for `barrow.json`. To add an
adventure, add its document here. [`builtin_adventures`][bxapi.adventures.builtin_adventures]
loads and validates every file.

`barrow.json` is *The Barrow of the Forgotten King*, the example adventure from osrlib's
`examples/tui_crawler/content.py`, written out as a document.
"""
