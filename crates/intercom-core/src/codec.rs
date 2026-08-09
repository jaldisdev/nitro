//! MessagePack encoding for messages crossing the wire.
//!
//! Messages are self-describing values rather than a fixed schema, so a sender
//! and a receiver written at different times can still understand each other as
//! long as they agree on what the fields mean. MessagePack is used rather than
//! a language-specific format because anything on either side of a channel may
//! be written in a different language.

use bytes::Bytes;
use serde::Serialize;
use serde::de::DeserializeOwned;

/// A decoded message. Values are dynamic: maps, arrays, strings, numbers,
/// booleans, binary blobs and nil.
pub type Value = rmpv::Value;

#[derive(Debug, thiserror::Error)]
pub enum CodecError {
    #[error("the message could not be encoded: {0}")]
    Encode(String),
    #[error("the message could not be decoded: {0}")]
    Decode(String),
}

/// Encode a dynamic value.
pub fn encode(value: &Value) -> Result<Bytes, CodecError> {
    let mut buffer = Vec::new();
    rmpv::encode::write_value(&mut buffer, value)
        .map_err(|error| CodecError::Encode(error.to_string()))?;
    Ok(Bytes::from(buffer))
}

/// Decode a dynamic value.
///
/// Trailing bytes are an error rather than something to ignore: a message that
/// does not account for all of its own bytes is not a message this understood.
pub fn decode(bytes: &[u8]) -> Result<Value, CodecError> {
    let mut cursor = std::io::Cursor::new(bytes);
    let value = rmpv::decode::read_value(&mut cursor)
        .map_err(|error| CodecError::Decode(error.to_string()))?;

    let consumed = cursor.position() as usize;
    if consumed != bytes.len() {
        return Err(CodecError::Decode(format!(
            "{} trailing byte(s) after the message",
            bytes.len() - consumed
        )));
    }
    Ok(value)
}

/// Encode anything serialisable, for callers with a type of their own.
pub fn encode_as<T: Serialize>(value: &T) -> Result<Bytes, CodecError> {
    rmp_serde::to_vec_named(value)
        .map(Bytes::from)
        .map_err(|error| CodecError::Encode(error.to_string()))
}

/// Decode into a type of the caller's choosing.
pub fn decode_into<T: DeserializeOwned>(bytes: &[u8]) -> Result<T, CodecError> {
    rmp_serde::from_slice(bytes).map_err(|error| CodecError::Decode(error.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(value: &str) -> Value {
        Value::String(value.into())
    }

    #[test]
    fn a_map_survives_a_round_trip() {
        let message = Value::Map(vec![
            (text("event"), text("joined")),
            (text("user"), text("ada")),
            (text("count"), Value::Integer(3.into())),
        ]);

        let encoded = encode(&message).unwrap();
        assert_eq!(decode(&encoded).unwrap(), message);
    }

    #[test]
    fn every_kind_of_value_survives_a_round_trip() {
        for value in [
            Value::Nil,
            Value::Boolean(true),
            Value::Integer((-7).into()),
            Value::F64(1.5),
            text("hello"),
            Value::Binary(vec![0, 1, 2, 255]),
            Value::Array(vec![Value::Integer(1.into()), text("two")]),
        ] {
            let encoded = encode(&value).unwrap();
            assert_eq!(decode(&encoded).unwrap(), value, "round trip of {value:?}");
        }
    }

    #[test]
    fn nesting_survives_a_round_trip() {
        let message = Value::Map(vec![(
            text("outer"),
            Value::Map(vec![(text("inner"), Value::Array(vec![text("deep")]))]),
        )]);
        assert_eq!(decode(&encode(&message).unwrap()).unwrap(), message);
    }

    #[test]
    fn text_that_is_not_ascii_survives() {
        let message = text("Grüße, 世界 🎉");
        assert_eq!(decode(&encode(&message).unwrap()).unwrap(), message);
    }

    #[test]
    fn an_empty_input_is_a_decode_error() {
        assert!(matches!(decode(&[]), Err(CodecError::Decode(_))));
    }

    #[test]
    fn a_truncated_message_is_a_decode_error() {
        let encoded = encode(&text("a reasonably long string")).unwrap();
        let truncated = &encoded[..encoded.len() / 2];
        assert!(matches!(decode(truncated), Err(CodecError::Decode(_))));
    }

    #[test]
    fn trailing_bytes_are_refused() {
        let mut encoded = encode(&text("hello")).unwrap().to_vec();
        encoded.push(0xff);

        let error = decode(&encoded).expect_err("trailing bytes must not be ignored");
        assert!(error.to_string().contains("trailing"));
    }

    #[test]
    fn a_typed_value_survives_a_round_trip() {
        #[derive(Debug, PartialEq, serde::Serialize, serde::Deserialize)]
        struct Joined {
            user: String,
            room: u32,
        }

        let message = Joined {
            user: "ada".to_owned(),
            room: 42,
        };
        let encoded = encode_as(&message).unwrap();
        assert_eq!(decode_into::<Joined>(&encoded).unwrap(), message);
    }

    #[test]
    fn a_typed_value_is_readable_as_a_dynamic_map() {
        #[derive(serde::Serialize)]
        struct Joined {
            user: String,
        }

        let encoded = encode_as(&Joined {
            user: "ada".to_owned(),
        })
        .unwrap();

        // Encoding with field names is what makes this possible; a positional
        // encoding would arrive as an array and lose them.
        let Value::Map(entries) = decode(&encoded).unwrap() else {
            panic!("a struct should decode as a map");
        };
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].0, text("user"));
    }

    #[test]
    fn decoding_into_the_wrong_type_is_an_error() {
        let encoded = encode(&text("not a number")).unwrap();
        assert!(decode_into::<u32>(&encoded).is_err());
    }
}
