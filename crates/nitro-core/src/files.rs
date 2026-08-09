//! Serving files, whole or in part.
//!
//! A file is read as it is sent rather than loaded first, so the memory a
//! response costs does not depend on the size of the file.
//!
//! Range requests are checked against the file's actual size. A range that
//! starts past the end of the file is unsatisfiable and is answered as such —
//! not with an empty body and a success status, which tells a client its range
//! was honoured when it was not.

use std::io;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::task::{Context, Poll};
use std::time::SystemTime;

use bytes::Bytes;
use futures_util::Stream;
use http_body::{Body, Frame, SizeHint};
use tokio::io::AsyncSeekExt;
use tokio_util::io::ReaderStream;

use crate::streaming::StreamError;

#[derive(Debug, thiserror::Error)]
pub enum FileError {
    #[error("{path} could not be opened: {source}")]
    Open {
        path: PathBuf,
        #[source]
        source: io::Error,
    },
    #[error("{path} is not a regular file")]
    NotAFile { path: PathBuf },
}

impl FileError {
    /// Whether the file simply is not there, which usually deserves a 404
    /// rather than a 500.
    pub fn is_not_found(&self) -> bool {
        match self {
            Self::Open { source, .. } => source.kind() == io::ErrorKind::NotFound,
            Self::NotAFile { .. } => false,
        }
    }
}

/// What a byte range resolves to once the file's size is known.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ResolvedRange {
    /// The whole file, because no usable range was asked for.
    Full { size: u64 },
    /// A part of the file. `end` is inclusive, as it is on the wire.
    Partial { start: u64, end: u64, size: u64 },
    /// The range cannot be satisfied and the response should say so.
    Unsatisfiable { size: u64 },
}

impl ResolvedRange {
    /// The number of bytes the response body will carry.
    pub fn length(self) -> u64 {
        match self {
            Self::Full { size } => size,
            Self::Partial { start, end, .. } => end - start + 1,
            Self::Unsatisfiable { .. } => 0,
        }
    }

    /// The value for a `Content-Range` header, if one belongs on the response.
    pub fn content_range(self) -> Option<String> {
        match self {
            Self::Full { .. } => None,
            Self::Partial { start, end, size } => Some(format!("bytes {start}-{end}/{size}")),
            // A rejected range still reports the size, which is how a client
            // learns what it should have asked for.
            Self::Unsatisfiable { size } => Some(format!("bytes */{size}")),
        }
    }
}

/// Resolve a requested range against a file of `size` bytes.
///
/// `start` and `end` follow HTTP: `end` is inclusive, and an absent `end` means
/// "to the last byte". A `start` beyond the last byte cannot be satisfied, and
/// neither can a range on an empty file. An `end` past the last byte is clamped
/// rather than rejected, because asking for more than exists is a normal way to
/// ask for the rest.
pub fn resolve_range(start: Option<u64>, end: Option<u64>, size: u64) -> ResolvedRange {
    let Some(start) = start else {
        return match end {
            // A bare end with no start is not a form this takes; treat it as
            // asking for the whole file rather than guessing.
            Some(_) | None => ResolvedRange::Full { size },
        };
    };

    if size == 0 || start >= size {
        return ResolvedRange::Unsatisfiable { size };
    }

    let last = size - 1;
    let end = end.unwrap_or(last).min(last);
    if end < start {
        return ResolvedRange::Unsatisfiable { size };
    }

    ResolvedRange::Partial { start, end, size }
}

/// A file that has been opened and measured, ready to be sent.
#[derive(Debug)]
pub struct OpenFile {
    pub path: PathBuf,
    pub size: u64,
    pub modified: Option<SystemTime>,
    pub content_type: String,
    file: tokio::fs::File,
}

impl OpenFile {
    pub async fn open(path: impl AsRef<Path>) -> Result<Self, FileError> {
        let path = path.as_ref().to_path_buf();
        let file = tokio::fs::File::open(&path)
            .await
            .map_err(|source| FileError::Open {
                path: path.clone(),
                source,
            })?;

        let metadata = file.metadata().await.map_err(|source| FileError::Open {
            path: path.clone(),
            source,
        })?;
        if !metadata.is_file() {
            return Err(FileError::NotAFile { path });
        }

        let content_type = mime_guess::from_path(&path)
            .first_or_octet_stream()
            .to_string();

        Ok(Self {
            size: metadata.len(),
            modified: metadata.modified().ok(),
            content_type,
            path,
            file,
        })
    }

    /// The `Last-Modified` header value, when the filesystem reports one.
    pub fn last_modified(&self) -> Option<String> {
        self.modified.map(httpdate::fmt_http_date)
    }

    /// Turn the file into a body carrying `range`.
    ///
    /// An unsatisfiable range produces an empty body; the caller is responsible
    /// for pairing it with the right status.
    pub async fn into_body(mut self, range: ResolvedRange) -> Result<FileBody, FileError> {
        if let ResolvedRange::Partial { start, .. } = range
            && start > 0
        {
            self.file
                .seek(io::SeekFrom::Start(start))
                .await
                .map_err(|source| FileError::Open {
                    path: self.path.clone(),
                    source,
                })?;
        }

        Ok(FileBody {
            remaining: range.length(),
            stream: ReaderStream::new(self.file),
        })
    }
}

/// A response body that reads from a file as it is sent.
#[derive(Debug)]
pub struct FileBody {
    remaining: u64,
    stream: ReaderStream<tokio::fs::File>,
}

impl FileBody {
    pub fn remaining(&self) -> u64 {
        self.remaining
    }
}

impl Body for FileBody {
    type Data = Bytes;
    type Error = StreamError;

    fn poll_frame(
        self: Pin<&mut Self>,
        context: &mut Context<'_>,
    ) -> Poll<Option<Result<Frame<Self::Data>, Self::Error>>> {
        let this = self.get_mut();
        if this.remaining == 0 {
            return Poll::Ready(None);
        }

        match Pin::new(&mut this.stream).poll_next(context) {
            Poll::Ready(Some(Ok(mut chunk))) => {
                // The reader does not know where the range ends, so the last
                // chunk is trimmed to it.
                if chunk.len() as u64 > this.remaining {
                    chunk.truncate(this.remaining as usize);
                }
                this.remaining -= chunk.len() as u64;
                Poll::Ready(Some(Ok(Frame::data(chunk))))
            }
            Poll::Ready(Some(Err(error))) => Poll::Ready(Some(Err(StreamError::Aborted(format!(
                "reading the file failed: {error}"
            ))))),
            Poll::Ready(None) => {
                if this.remaining > 0 {
                    tracing::warn!(
                        missing = this.remaining,
                        "the file ended before the response did; it changed while being sent"
                    );
                    this.remaining = 0;
                }
                Poll::Ready(None)
            }
            Poll::Pending => Poll::Pending,
        }
    }

    fn is_end_stream(&self) -> bool {
        self.remaining == 0
    }

    fn size_hint(&self) -> SizeHint {
        SizeHint::with_exact(self.remaining)
    }
}

#[cfg(test)]
mod tests {
    use http_body_util::BodyExt;

    use super::*;

    fn write(directory: &tempfile::TempDir, name: &str, contents: &[u8]) -> PathBuf {
        let path = directory.path().join(name);
        std::fs::write(&path, contents).unwrap();
        path
    }

    #[test]
    fn no_range_asks_for_the_whole_file() {
        assert_eq!(
            resolve_range(None, None, 100),
            ResolvedRange::Full { size: 100 }
        );
    }

    #[test]
    fn a_range_within_the_file_is_partial() {
        assert_eq!(
            resolve_range(Some(10), Some(19), 100),
            ResolvedRange::Partial {
                start: 10,
                end: 19,
                size: 100
            }
        );
    }

    #[test]
    fn an_open_ended_range_runs_to_the_last_byte() {
        assert_eq!(
            resolve_range(Some(90), None, 100),
            ResolvedRange::Partial {
                start: 90,
                end: 99,
                size: 100
            }
        );
    }

    #[test]
    fn an_end_past_the_file_is_clamped() {
        assert_eq!(
            resolve_range(Some(90), Some(1000), 100),
            ResolvedRange::Partial {
                start: 90,
                end: 99,
                size: 100
            }
        );
    }

    #[test]
    fn a_start_past_the_file_cannot_be_satisfied() {
        assert_eq!(
            resolve_range(Some(100), None, 100),
            ResolvedRange::Unsatisfiable { size: 100 }
        );
        assert_eq!(
            resolve_range(Some(500), Some(600), 100),
            ResolvedRange::Unsatisfiable { size: 100 }
        );
    }

    #[test]
    fn the_last_byte_is_reachable() {
        assert_eq!(
            resolve_range(Some(99), None, 100),
            ResolvedRange::Partial {
                start: 99,
                end: 99,
                size: 100
            }
        );
    }

    #[test]
    fn an_end_before_the_start_cannot_be_satisfied() {
        assert_eq!(
            resolve_range(Some(50), Some(10), 100),
            ResolvedRange::Unsatisfiable { size: 100 }
        );
    }

    #[test]
    fn an_empty_file_cannot_satisfy_any_range() {
        assert_eq!(
            resolve_range(Some(0), None, 0),
            ResolvedRange::Unsatisfiable { size: 0 }
        );
        assert_eq!(
            resolve_range(None, None, 0),
            ResolvedRange::Full { size: 0 }
        );
    }

    #[test]
    fn lengths_and_content_ranges_line_up() {
        let full = ResolvedRange::Full { size: 100 };
        assert_eq!(full.length(), 100);
        assert_eq!(full.content_range(), None);

        let partial = ResolvedRange::Partial {
            start: 10,
            end: 19,
            size: 100,
        };
        assert_eq!(partial.length(), 10);
        assert_eq!(partial.content_range().as_deref(), Some("bytes 10-19/100"));

        let rejected = ResolvedRange::Unsatisfiable { size: 100 };
        assert_eq!(rejected.length(), 0);
        assert_eq!(rejected.content_range().as_deref(), Some("bytes */100"));
    }

    #[tokio::test]
    async fn a_file_reports_its_size_and_type() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "page.html", b"<h1>hello</h1>");

        let opened = OpenFile::open(&path).await.unwrap();
        assert_eq!(opened.size, 14);
        assert_eq!(opened.content_type, "text/html");
        assert!(opened.last_modified().is_some());
    }

    #[tokio::test]
    async fn an_unknown_extension_falls_back_to_octet_stream() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "blob.unknownext", b"data");
        assert_eq!(
            OpenFile::open(&path).await.unwrap().content_type,
            "application/octet-stream"
        );
    }

    #[tokio::test]
    async fn a_missing_file_says_so() {
        let error = OpenFile::open("/nonexistent/file.txt").await.unwrap_err();
        assert!(error.is_not_found());
    }

    #[tokio::test]
    async fn a_directory_is_not_a_file() {
        let directory = tempfile::tempdir().unwrap();
        let error = OpenFile::open(directory.path()).await.unwrap_err();
        assert!(matches!(error, FileError::NotAFile { .. }));
        assert!(!error.is_not_found());
    }

    #[tokio::test]
    async fn the_whole_file_is_sent() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "data.txt", b"0123456789");

        let opened = OpenFile::open(&path).await.unwrap();
        let range = resolve_range(None, None, opened.size);
        let body = opened.into_body(range).await.unwrap();

        assert_eq!(&body.collect().await.unwrap().to_bytes()[..], b"0123456789");
    }

    #[tokio::test]
    async fn a_range_sends_exactly_the_requested_bytes() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "data.txt", b"0123456789");

        let opened = OpenFile::open(&path).await.unwrap();
        let range = resolve_range(Some(2), Some(5), opened.size);
        let body = opened.into_body(range).await.unwrap();

        assert_eq!(body.remaining(), 4);
        assert_eq!(&body.collect().await.unwrap().to_bytes()[..], b"2345");
    }

    #[tokio::test]
    async fn a_range_to_the_end_sends_the_tail() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "data.txt", b"0123456789");

        let opened = OpenFile::open(&path).await.unwrap();
        let range = resolve_range(Some(7), None, opened.size);
        let body = opened.into_body(range).await.unwrap();

        assert_eq!(&body.collect().await.unwrap().to_bytes()[..], b"789");
    }

    #[tokio::test]
    async fn an_unsatisfiable_range_produces_no_body() {
        let directory = tempfile::tempdir().unwrap();
        let path = write(&directory, "data.txt", b"0123456789");

        let opened = OpenFile::open(&path).await.unwrap();
        let range = resolve_range(Some(50), None, opened.size);
        let body = opened.into_body(range).await.unwrap();

        assert!(body.collect().await.unwrap().to_bytes().is_empty());
    }

    #[tokio::test]
    async fn a_large_file_is_read_in_several_chunks() {
        let directory = tempfile::tempdir().unwrap();
        let contents: Vec<u8> = (0..200_000).map(|index| (index % 251) as u8).collect();
        let path = write(&directory, "large.bin", &contents);

        let opened = OpenFile::open(&path).await.unwrap();
        let range = resolve_range(Some(1), Some(199_998), opened.size);
        let mut body = opened.into_body(range).await.unwrap();

        let mut chunks = 0;
        let mut collected = Vec::new();
        while let Some(frame) = std::pin::Pin::new(&mut body).frame().await {
            let data = frame.unwrap().into_data().unwrap();
            chunks += 1;
            collected.extend_from_slice(&data);
        }

        assert!(chunks > 1, "a large file should not arrive as one chunk");
        assert_eq!(collected, contents[1..=199_998]);
    }
}
