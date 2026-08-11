//
// This source file is part of the Nitro open source project.
//
// Copyright (c) 2026 Jaldis B.V.
//
// Licensed under the MIT OR Apache-2.0 license (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://opensource.org/licenses/MIT
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.
//

//! Channel-backed streaming response bodies.
//!
//! The channel is bounded. Once the transport is that many chunks behind, a
//! producer that tries to send another one waits until a chunk has actually
//! been written. This is what makes the send operation meaningful as an
//! awaitable: a producer faster than the client is slowed to the client's pace
//! rather than filling memory with queued chunks.

use std::pin::Pin;
use std::task::{Context, Poll};

use bytes::Bytes;
use http_body::{Body, Frame, SizeHint};
use tokio::sync::mpsc;

#[derive(Debug, thiserror::Error)]
pub enum StreamError {
    #[error("the receiving end of the response stream is gone")]
    Closed,
    #[error("the response stream was aborted: {0}")]
    Aborted(String),
}

/// Producer half of a streaming response body.
#[derive(Debug, Clone)]
pub struct StreamSender {
    sender: mpsc::Sender<Result<Bytes, StreamError>>,
}

impl StreamSender {
    /// Queue a chunk, waiting while the channel is full.
    ///
    /// Returns [`StreamError::Closed`] once the transport has gone away, which
    /// is the producer's cue to stop generating data.
    pub async fn send(&self, chunk: Bytes) -> Result<(), StreamError> {
        self.sender
            .send(Ok(chunk))
            .await
            .map_err(|_| StreamError::Closed)
    }

    /// Queue a chunk only if there is room right now.
    pub fn try_send(&self, chunk: Bytes) -> Result<(), TrySendError> {
        match self.sender.try_send(Ok(chunk)) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(_)) => Err(TrySendError::Full),
            Err(mpsc::error::TrySendError::Closed(_)) => Err(TrySendError::Closed),
        }
    }

    /// End the body with an error so the client sees a truncated response
    /// rather than a complete one.
    pub async fn abort(&self, reason: impl Into<String>) {
        let error = StreamError::Aborted(reason.into());
        if self.sender.send(Err(error)).await.is_err() {
            tracing::debug!("stream aborted after the transport had already closed");
        }
    }

    /// Whether the transport has stopped reading.
    pub fn is_closed(&self) -> bool {
        self.sender.is_closed()
    }

    /// Resolve when the transport stops reading, so a producer blocked on
    /// something other than the channel can notice.
    pub async fn closed(&self) {
        self.sender.closed().await;
    }

    /// Chunks that fit before [`send`] starts waiting.
    ///
    /// [`send`]: Self::send
    pub fn capacity(&self) -> usize {
        self.sender.capacity()
    }
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum TrySendError {
    #[error("the response stream buffer is full")]
    Full,
    #[error("the receiving end of the response stream is gone")]
    Closed,
}

/// Consumer half, handed to the transport as the response body.
#[derive(Debug)]
pub struct StreamBody {
    receiver: mpsc::Receiver<Result<Bytes, StreamError>>,
}

impl Body for StreamBody {
    type Data = Bytes;
    type Error = StreamError;

    fn poll_frame(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        match self.get_mut().receiver.poll_recv(context) {
            Poll::Ready(Some(Ok(chunk))) => Poll::Ready(Some(Ok(Frame::data(chunk)))),
            Poll::Ready(Some(Err(error))) => Poll::Ready(Some(Err(error))),
            Poll::Ready(None) => Poll::Ready(None),
            Poll::Pending => Poll::Pending,
        }
    }

    fn is_end_stream(&self) -> bool {
        false
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::default()
    }
}

/// Create a streaming body with room for `capacity` queued chunks.
///
/// A capacity of zero would deadlock, so it is raised to one.
pub fn channel(capacity: usize) -> (StreamSender, StreamBody) {
    let (sender, receiver) = mpsc::channel(capacity.max(1));
    (StreamSender { sender }, StreamBody { receiver })
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use http_body_util::BodyExt;

    use super::*;

    #[tokio::test]
    async fn chunks_arrive_in_order() {
        let (sender, body) = channel(4);
        tokio::spawn(async move {
            for chunk in ["one", "two", "three"] {
                sender
                    .send(Bytes::from_static(chunk.as_bytes()))
                    .await
                    .unwrap();
            }
        });

        let collected = body
            .collect()
            .await
            .expect("stream must complete")
            .to_bytes();
        assert_eq!(&collected[..], b"onetwothree");
    }

    #[tokio::test]
    async fn sending_waits_once_the_buffer_is_full() {
        let (sender, mut body) = channel(2);
        sender.send(Bytes::from_static(b"a")).await.unwrap();
        sender.send(Bytes::from_static(b"b")).await.unwrap();

        let blocked = sender.send(Bytes::from_static(b"c"));
        tokio::pin!(blocked);
        assert!(
            tokio::time::timeout(Duration::from_millis(50), &mut blocked)
                .await
                .is_err(),
            "a full buffer must make the producer wait"
        );

        let frame = Pin::new(&mut body)
            .frame()
            .await
            .expect("a frame is queued");
        assert_eq!(
            frame.unwrap().into_data().unwrap(),
            Bytes::from_static(b"a")
        );

        tokio::time::timeout(Duration::from_millis(500), blocked)
            .await
            .expect("draining a chunk must let the producer continue")
            .expect("send must succeed");
    }

    #[tokio::test]
    async fn try_send_reports_a_full_buffer_instead_of_waiting() {
        let (sender, _body) = channel(1);
        assert_eq!(sender.try_send(Bytes::from_static(b"a")), Ok(()));
        assert_eq!(
            sender.try_send(Bytes::from_static(b"b")),
            Err(TrySendError::Full)
        );
    }

    #[tokio::test]
    async fn dropping_the_body_stops_the_producer() {
        let (sender, body) = channel(1);
        drop(body);

        assert!(sender.is_closed());
        assert!(matches!(
            sender.send(Bytes::from_static(b"a")).await,
            Err(StreamError::Closed)
        ));
        assert_eq!(
            sender.try_send(Bytes::from_static(b"a")),
            Err(TrySendError::Closed)
        );
    }

    #[tokio::test]
    async fn closed_resolves_when_the_transport_goes_away() {
        let (sender, body) = channel(1);
        let observer = tokio::spawn(async move { sender.closed().await });
        tokio::task::yield_now().await;
        drop(body);

        tokio::time::timeout(Duration::from_secs(1), observer)
            .await
            .expect("closed() must resolve when the body is dropped")
            .expect("observer task panicked");
    }

    #[tokio::test]
    async fn an_abort_surfaces_as_a_body_error() {
        let (sender, body) = channel(4);
        sender.send(Bytes::from_static(b"partial")).await.unwrap();
        sender.abort("upstream failed").await;
        drop(sender);

        let error = body.collect().await.expect_err("the body must fail");
        assert!(matches!(error, StreamError::Aborted(reason) if reason == "upstream failed"));
    }

    #[tokio::test]
    async fn dropping_the_sender_ends_the_body() {
        let (sender, body) = channel(4);
        drop(sender);
        let collected = body
            .collect()
            .await
            .expect("an empty stream is valid")
            .to_bytes();
        assert!(collected.is_empty());
    }

    #[tokio::test]
    async fn zero_capacity_is_raised_so_a_lone_producer_cannot_deadlock() {
        let (sender, mut body) = channel(0);
        assert_eq!(sender.capacity(), 1);
        sender.send(Bytes::from_static(b"a")).await.unwrap();
        let frame = Pin::new(&mut body)
            .frame()
            .await
            .expect("a frame is queued");
        assert_eq!(
            frame.unwrap().into_data().unwrap(),
            Bytes::from_static(b"a")
        );
    }
}
