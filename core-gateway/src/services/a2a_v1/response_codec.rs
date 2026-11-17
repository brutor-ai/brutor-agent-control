// Copyright 2026 Brutor AI Ltd.
// SPDX-License-Identifier: Apache-2.0

//! Custom serde codec for [`SendMessageResponse`] and [`StreamResponse`].
//!
//! Both are proto oneofs. proto3-JSON lifts the selected variant to a
//! top-level field with the oneof field name. For `SendMessageResponse`:
//!
//! * `{"task": {…}}`  — [`SendMessageResponse::Task`]
//! * `{"message": {…}}` — [`SendMessageResponse::Message`]
//!
//! For `StreamResponse`:
//!
//! * `{"task": {…}}` — [`StreamResponse::Task`]
//! * `{"message": {…}}` — [`StreamResponse::Message`]
//! * `{"statusUpdate": {…}}` — [`StreamResponse::StatusUpdate`]
//! * `{"artifactUpdate": {…}}` — [`StreamResponse::ArtifactUpdate`]
//!
//! Unlike [`super::Part`], these oneofs have no shared sibling fields;
//! the envelope is exactly one key. We still reject unknown keys and
//! multi-key objects to catch wire-level bugs early.

use std::fmt;

use serde::de::{Error as _, MapAccess, Visitor};
use serde::ser::{SerializeMap, Serializer};
use serde::{Deserialize, Deserializer, Serialize};

use super::{
    Message, SendMessageResponse, StreamResponse, Task, TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
};

// =============================================================================
// SendMessageResponse
// =============================================================================

impl Serialize for SendMessageResponse {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut map = s.serialize_map(Some(1))?;
        match self {
            SendMessageResponse::Task(t) => map.serialize_entry("task", t)?,
            SendMessageResponse::Message(m) => map.serialize_entry("message", m)?,
        }
        map.end()
    }
}

impl<'de> Deserialize<'de> for SendMessageResponse {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_map(SendMessageResponseVisitor)
    }
}

struct SendMessageResponseVisitor;

impl<'de> Visitor<'de> for SendMessageResponseVisitor {
    type Value = SendMessageResponse;

    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("SendMessageResponse with exactly one of task/message")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut chosen: Option<SendMessageResponse> = None;
        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "task" => {
                    if chosen.is_some() {
                        return Err(M::Error::custom(
                            "SendMessageResponse must have exactly one of task/message",
                        ));
                    }
                    chosen = Some(SendMessageResponse::Task(map.next_value::<Task>()?));
                }
                "message" => {
                    if chosen.is_some() {
                        return Err(M::Error::custom(
                            "SendMessageResponse must have exactly one of task/message",
                        ));
                    }
                    chosen = Some(SendMessageResponse::Message(map.next_value::<Message>()?));
                }
                other => {
                    return Err(M::Error::unknown_field(other, &["task", "message"]));
                }
            }
        }
        chosen.ok_or_else(|| {
            M::Error::custom("SendMessageResponse missing payload (expected task or message)")
        })
    }
}

// =============================================================================
// StreamResponse
// =============================================================================

impl Serialize for StreamResponse {
    fn serialize<S: Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        let mut map = s.serialize_map(Some(1))?;
        match self {
            StreamResponse::Task(t) => map.serialize_entry("task", t)?,
            StreamResponse::Message(m) => map.serialize_entry("message", m)?,
            StreamResponse::StatusUpdate(s) => map.serialize_entry("statusUpdate", s)?,
            StreamResponse::ArtifactUpdate(a) => map.serialize_entry("artifactUpdate", a)?,
        }
        map.end()
    }
}

impl<'de> Deserialize<'de> for StreamResponse {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_map(StreamResponseVisitor)
    }
}

struct StreamResponseVisitor;

impl<'de> Visitor<'de> for StreamResponseVisitor {
    type Value = StreamResponse;

    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("StreamResponse with exactly one of task/message/statusUpdate/artifactUpdate")
    }

    fn visit_map<M: MapAccess<'de>>(self, mut map: M) -> Result<Self::Value, M::Error> {
        let mut chosen: Option<StreamResponse> = None;
        while let Some(key) = map.next_key::<String>()? {
            if chosen.is_some() {
                return Err(M::Error::custom(
                    "StreamResponse must have exactly one oneof payload",
                ));
            }
            chosen = Some(match key.as_str() {
                "task" => StreamResponse::Task(map.next_value::<Task>()?),
                "message" => StreamResponse::Message(map.next_value::<Message>()?),
                "statusUpdate" => {
                    StreamResponse::StatusUpdate(map.next_value::<TaskStatusUpdateEvent>()?)
                }
                "artifactUpdate" => {
                    StreamResponse::ArtifactUpdate(map.next_value::<TaskArtifactUpdateEvent>()?)
                }
                other => {
                    return Err(M::Error::unknown_field(
                        other,
                        &["task", "message", "statusUpdate", "artifactUpdate"],
                    ));
                }
            });
        }
        chosen.ok_or_else(|| {
            M::Error::custom(
                "StreamResponse missing payload (expected task/message/statusUpdate/artifactUpdate)",
            )
        })
    }
}
