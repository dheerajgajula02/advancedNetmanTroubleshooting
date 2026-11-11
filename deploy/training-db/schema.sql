-- Training data for the troubleshooting AI, laid out like the hackathon's training DB.
-- Raw facts only: any reward, SFT or next-call format is derived later from these tables, so the method can change.

-- Every investigation, whether or not an AI touched it: the whole stored record.
CREATE TABLE runs (
    run_id       text PRIMARY KEY,
    happened_at  timestamptz NOT NULL,
    device       text,
    event        text,
    state        text,
    handled_by   text,
    label        text,
    tags         text[] NOT NULL DEFAULT '{}',
    record       jsonb NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON runs (happened_at DESC);

-- One model working one run: an AI takeover, or the short written read of a run.
CREATE TABLE episodes (
    episode_id   uuid PRIMARY KEY,
    run_id       text,
    kind         text NOT NULL CHECK (kind IN ('agent', 'analyst', 'other', 'desk', 'notes', 'person')),
    capture      text NOT NULL DEFAULT 'live' CHECK (capture IN ('live', 'backfill')),  -- backfill: rebuilt from an older record, so thinking is partial
    model        text,
    settings     jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'running',
    outcome      jsonb,
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz
);
CREATE INDEX ON episodes (started_at DESC);
CREATE INDEX ON episodes (run_id);

-- What the model was given when it started, section by section.
-- body is the text exactly as sent. items is the structured data behind it (retrieved documents with their scores, and so on).
-- sent is false for things kept for training that the model did not read, such as the full run record.
CREATE TABLE inputs (
    episode_id   uuid NOT NULL REFERENCES episodes ON DELETE CASCADE,
    section_idx  int NOT NULL,
    section      text NOT NULL,
    source       text NOT NULL,   -- instructions, tools, incident, rules, retrieved, live, snapshot
    sent         boolean NOT NULL DEFAULT true,
    chars        int NOT NULL DEFAULT 0,
    body         text,
    items        jsonb,
    PRIMARY KEY (episode_id, section_idx)
);

-- The conversation exactly as the model saw and produced it, in order. thinking is the provider's reasoning text, uncut.
CREATE TABLE messages (
    episode_id   uuid NOT NULL REFERENCES episodes ON DELETE CASCADE,
    idx          int NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    role         text NOT NULL,
    content      text,
    thinking     text,
    tool_calls   jsonb,
    tool_call_id text,
    PRIMARY KEY (episode_id, idx)
);

-- Each request to the model and its full reply.
CREATE TABLE llm_calls (
    episode_id   uuid NOT NULL REFERENCES episodes ON DELETE CASCADE,
    call_idx     int NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    messages_sent int NOT NULL,
    thinking     text,
    content      text,
    finish       text,
    usage        jsonb,
    latency_ms   int,
    request      jsonb,          -- model, temperature, max_tokens, hash of the tool list
    raw_response jsonb,
    PRIMARY KEY (episode_id, call_idx)
);

-- Every tool call: why the model said it was running it, what it ran, and what came back.
CREATE TABLE steps (
    episode_id   uuid NOT NULL REFERENCES episodes ON DELETE CASCADE,
    step_idx     int NOT NULL,
    ts           timestamptz NOT NULL DEFAULT now(),
    llm_call     int,            -- the llm_calls row whose thinking led to this
    reason       text,           -- the model's stated reason for this command
    action       jsonb,          -- {tool, args}
    kind         text,           -- read, change, propose, finish
    outcome      text,
    saw          text,
    tool_output  text,           -- the full output
    given_back   text,           -- exactly what was returned to the model
    validation   jsonb,          -- commands actually run, and for a change whether it was kept or undone
    latency_ms   int,
    PRIMARY KEY (episode_id, step_idx)
);

-- Network state around the AI's work.
CREATE TABLE state_snapshots (
    snapshot_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       text,
    episode_id   uuid REFERENCES episodes ON DELETE CASCADE,
    step_idx     int,
    phase        text NOT NULL,  -- handover, pre_change, post_change, final
    ts           timestamptz NOT NULL DEFAULT now(),
    state        jsonb NOT NULL
);
CREATE INDEX ON state_snapshots (episode_id, phase);

-- Judgements made afterwards: how the session ended, what a person accepted or rejected and why.
CREATE TABLE labels (
    label_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id       text,
    episode_id   uuid REFERENCES episodes ON DELETE CASCADE,
    step_idx     int,
    labeler      text NOT NULL,  -- system, person
    label        jsonb NOT NULL,
    dedupe       text UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON labels (run_id);

-- Ground truth from the fault drills. Never shown to the AI: it lives here and nowhere the AI can read.
CREATE TABLE fault_truth (
    fault_id     uuid PRIMARY KEY,
    drill        text NOT NULL,
    spec         jsonb NOT NULL,
    injected_at  timestamptz NOT NULL,
    cleared_at   timestamptz
);
CREATE INDEX ON fault_truth (injected_at);
