# SPEC for nano cluster and Std.IO universal interface

## Concepts

- orchestrator/dispatcher - is the process that forks all other processes and passes messages between them
- node - a host on which the dispatcher is running
- namespace - a group of stacks
- stack - a group of modules
- module - a nano service template which is replicated into one or more units
- unit - a single process which have its stdio attached to the orchestrator/dispatcher, a module might have multiple units
- fully isolated module - a module with no network and no filesystem access except `/tmp`
- relay / adapter - a special module that have network access or filesystem access ex. http relay or db relay

## Message Format

a message start with a single character that identify the entire message format to be one of the following

- `{` followed by the rest of the JSON line.
- `j` followed by 32-bit little-endian integer size followed by *JSON* message
- `m` followed by 32-bit little-endian integer size followed by *msgPack* message
- `t` followed by 32-bit little-endian integer size followed by *compact thrift* message
- `p` followed by 32-bit little-endian integer size followed by *protocol buffer* message
- `f` followed by 32-bit little-endian integer size followed by *flat buffer* message

## Fully Qualified Method Name

The format is `<namespace>.<stack>.<module>.<method>` for example:

- `my_namespace.my_stack.blog.add_article`
- `my_namespace.my_stack._manage.module_config`
- `my_namespace.my_stack._manage.set_replica`
- `my_namespace.new_stack._manage.launch`
- `global.global.db.fetch_all`

any missing part is assumed to be relative to current.

## Message Types

### Invoking A Method

only method is required

- `>> {id:"abc-123", method: "", params: {...}}` invoking a method
- `>> {id:null, method: "", params: {...}}` invoking a fire-and-forget method
- `>> {id:"", method: "", params: {...}}` invoking a method and auto-generate id

### Receiving Results

when method is missing, a non-null `error` property indicates a failure. otherwise result will hold the returned value

- `<< {id:"abc-123", error: {codename:"", message:"", data:{...}}}` - failed result
- `<< {id:"abc-123", result: {...}}` success result aka. return value

### Event Stream

some methods (ex. `subscribe` or `fetch_iter`) can return an async generator or a full-duplex event stream
this is indicated by `cursor_id` (unlike `id` which is set by caller)
the difference between an async generator indicated by `_started` response (ex. `get_iter`) and an event stream indicated by `_est` response (ex. `pubsub`) is that the generator requires the requests to keep responding with event named `"_next"`
while the event steam does not wait for `_next`

### Async Generator

- `<< {id:"abc-123", cursor_id: "9876abc", event: "_started", data: {meta:{...}...}}`
- `<< {id:"abc-123", cursor_id: "9876abc", event: "_batch", data: {items:[...]...}}` - batch may arrive multiple times
- `>> {id:"abc-123", cursor_id: "9876abc", event: "_next"}`
- `<< {id:"abc-123", cursor_id: "9876abc", event: "_batch", data: {items:[...]...}}` - batch may arrive multiple times
- `...`
- `<< {id:"abc-123", cursor_id: "9876abc", event: "_done"}`

### Stream aka `pubsub`

- `<< {id:"abc-123", cursor_id: "9876abc", event: "_est", data: {meta:{...}...}}`
- `<< {id:"abc-123", cursor_id: "9876abc", event: "my_event", data: {items:[...]...}}` - multiple events arrive, event names are custom
- `>> {id:"abc-123", cursor_id: "9876abc", event: "my_other_event", data: {items:[...]...}}` - full duplex
- `<< {id:"abc-123", cursor_id: "9876abc", event: "_done"}`


### Leaving the Stream

both types can be left by calling `_cancel`

- `>> {id:"abc-123”, cursor_id: "9876abc", event: "_cancel"}`
- `<< {id:"abc-123”, cursor_id: "9876abc", event: "_canceled", data:{}}` - in case of error or in case of other party cancel
- `<< {id:"abc-123", cursor_id: "9876abc", event: "_done"}` - done is fired after canceled to do cleanup


