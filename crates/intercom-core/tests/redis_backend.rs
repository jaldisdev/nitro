//! Tests against a real Redis.
//!
//! Each test works under a prefix of its own so they cannot see one another,
//! and the whole file skips rather than fails when no server is reachable —
//! a developer without Redis running should not be stopped by it, while CI,
//! which has one, still gets the coverage.

use std::time::Duration;

use bytes::Bytes;
use intercom_core::channel::{ChannelConfig, unique_channel};
use intercom_core::codec::{self, Value};
use intercom_core::redis::Intercom;

const URL: &str = "redis://127.0.0.1:6379";

/// Connect under a prefix nothing else uses, or skip.
async fn connect(test: &str) -> Option<Intercom> {
    let config = ChannelConfig {
        prefix: format!("nitro-test:{}:{test}", unique_channel("run")),
        capacity: 4,
        expiry: Duration::from_secs(30),
    };

    match Intercom::connect(URL, config).await {
        Ok(intercom) => match intercom.ping().await {
            Ok(()) => Some(intercom),
            Err(_) => None,
        },
        Err(_) => None,
    }
}

macro_rules! intercom {
    ($test:expr) => {
        match connect($test).await {
            Some(intercom) => intercom,
            None => {
                eprintln!("skipping {}: no Redis at {URL}", $test);
                return;
            }
        }
    };
}

fn message(text: &str) -> Bytes {
    codec::encode(&Value::Map(vec![(
        Value::String("text".into()),
        Value::String(text.into()),
    )]))
    .unwrap()
}

fn text_of(payload: &Bytes) -> String {
    let Value::Map(entries) = codec::decode(payload).unwrap() else {
        panic!("expected a map");
    };
    entries[0].1.as_str().unwrap().to_owned()
}

#[tokio::test]
async fn a_queued_message_is_read_back() {
    let intercom = intercom!("queue-round-trip");

    intercom.send("room", message("hello")).await.unwrap();
    let received = intercom.receive("room").await.unwrap().expect("a message");

    assert_eq!(text_of(&received), "hello");
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn an_empty_channel_reads_as_nothing() {
    let intercom = intercom!("queue-empty");
    assert!(intercom.receive("quiet").await.unwrap().is_none());
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn queued_messages_are_read_oldest_first() {
    let intercom = intercom!("queue-order");

    for index in 0..3 {
        intercom
            .send("room", message(&format!("message-{index}")))
            .await
            .unwrap();
    }

    for index in 0..3 {
        let received = intercom.receive("room").await.unwrap().expect("a message");
        assert_eq!(text_of(&received), format!("message-{index}"));
    }
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_full_channel_discards_the_oldest() {
    let intercom = intercom!("queue-capacity");

    // The configured capacity is four.
    for index in 0..6 {
        intercom
            .send("room", message(&format!("message-{index}")))
            .await
            .unwrap();
    }

    let mut received = Vec::new();
    while let Some(payload) = intercom.receive("room").await.unwrap() {
        received.push(text_of(&payload));
    }

    assert_eq!(
        received,
        vec!["message-2", "message-3", "message-4", "message-5"],
        "the newest messages are the ones kept"
    );
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_blocking_read_waits_for_a_message() {
    let intercom = intercom!("queue-blocking");
    let writer = intercom.clone();

    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(100)).await;
        writer.send("room", message("late")).await.unwrap();
    });

    let mut reader = intercom.reader("room").await.unwrap();
    let received = reader
        .next_message(Duration::from_secs(5))
        .await
        .unwrap()
        .expect("the message must arrive");

    assert_eq!(text_of(&received), "late");
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_blocking_read_gives_up_at_its_timeout() {
    let intercom = intercom!("queue-blocking-timeout");

    let mut reader = intercom.reader("quiet").await.unwrap();
    let started = std::time::Instant::now();
    let received = reader.next_message(Duration::from_secs(1)).await.unwrap();

    assert!(received.is_none());
    assert!(started.elapsed() < Duration::from_secs(5));
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_published_message_reaches_a_subscriber() {
    let intercom = intercom!("publish-subscribe");

    let mut subscription = intercom.subscribe("live").await.unwrap();
    // Subscribing is not instantaneous on the server; publishing too early
    // would be delivered to nobody.
    tokio::time::sleep(Duration::from_millis(100)).await;

    intercom.publish("live", message("now")).await.unwrap();

    let received = tokio::time::timeout(Duration::from_secs(5), subscription.next_message())
        .await
        .expect("a message must arrive")
        .expect("the subscription must stay open");

    assert_eq!(text_of(&received), "now");
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn publishing_to_nobody_reaches_nobody() {
    let intercom = intercom!("publish-nobody");
    assert_eq!(intercom.publish("empty", message("lost")).await.unwrap(), 0);
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn every_subscriber_receives_a_published_message() {
    let intercom = intercom!("publish-fan-out");

    let mut first = intercom.subscribe("live").await.unwrap();
    let mut second = intercom.subscribe("live").await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    assert_eq!(intercom.publish("live", message("both")).await.unwrap(), 2);

    for subscription in [&mut first, &mut second] {
        let received = tokio::time::timeout(Duration::from_secs(5), subscription.next_message())
            .await
            .expect("a message must arrive")
            .expect("the subscription must stay open");
        assert_eq!(text_of(&received), "both");
    }
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn group_membership_is_recorded() {
    let intercom = intercom!("group-membership");

    intercom.group_add("room", "alice").await.unwrap();
    intercom.group_add("room", "bob").await.unwrap();
    // Adding twice does not add twice.
    intercom.group_add("room", "bob").await.unwrap();

    let mut channels = intercom.group_channels("room").await.unwrap();
    channels.sort();
    assert_eq!(channels, vec!["alice", "bob"]);
    assert_eq!(intercom.group_size("room").await.unwrap(), 2);

    assert!(intercom.group_discard("room", "bob").await.unwrap());
    assert!(
        !intercom.group_discard("room", "bob").await.unwrap(),
        "discarding what is not there reports so"
    );
    assert_eq!(
        intercom.group_channels("room").await.unwrap(),
        vec!["alice"]
    );

    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn an_unknown_group_is_empty_rather_than_an_error() {
    let intercom = intercom!("group-unknown");
    assert!(intercom.group_channels("nowhere").await.unwrap().is_empty());
    assert_eq!(intercom.group_size("nowhere").await.unwrap(), 0);
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_group_send_reaches_every_member() {
    let intercom = intercom!("group-send");

    intercom.group_add("room", "alice").await.unwrap();
    intercom.group_add("room", "bob").await.unwrap();
    intercom
        .group_send("room", message("everyone"))
        .await
        .unwrap();

    for member in ["alice", "bob"] {
        let received = intercom
            .receive(member)
            .await
            .unwrap()
            .unwrap_or_else(|| panic!("{member} should have a message"));
        assert_eq!(text_of(&received), "everyone");
    }
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_group_send_to_an_empty_group_does_nothing() {
    let intercom = intercom!("group-send-empty");
    intercom
        .group_send("nowhere", message("lost"))
        .await
        .unwrap();
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_group_publish_reaches_every_subscribed_member() {
    let intercom = intercom!("group-publish");

    intercom.group_add("room", "alice").await.unwrap();
    intercom.group_add("room", "bob").await.unwrap();

    let mut alice = intercom.subscribe("alice").await.unwrap();
    let mut bob = intercom.subscribe("bob").await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;

    assert_eq!(
        intercom
            .group_publish("room", message("live"))
            .await
            .unwrap(),
        2
    );

    for subscription in [&mut alice, &mut bob] {
        let received = tokio::time::timeout(Duration::from_secs(5), subscription.next_message())
            .await
            .expect("a message must arrive")
            .expect("the subscription must stay open");
        assert_eq!(text_of(&received), "live");
    }
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn flushing_removes_only_what_the_prefix_owns() {
    let Some(mine) = connect("flush-mine").await else {
        eprintln!("skipping flush-mine: no Redis at {URL}");
        return;
    };
    let Some(theirs) = connect("flush-theirs").await else {
        return;
    };

    mine.send("room", message("mine")).await.unwrap();
    theirs.send("room", message("theirs")).await.unwrap();

    assert_eq!(mine.flush().await.unwrap(), 1);
    assert!(mine.receive("room").await.unwrap().is_none());
    assert!(
        theirs.receive("room").await.unwrap().is_some(),
        "another prefix must be left alone"
    );

    theirs.flush().await.unwrap();
}

#[tokio::test]
async fn a_dedicated_reader_does_not_stall_the_shared_connection() {
    let intercom = intercom!("reader-isolation");

    // A reader waiting on an empty channel must not hold up anything else.
    let mut waiting = intercom.reader("quiet").await.unwrap();
    let waiter = tokio::spawn(async move { waiting.next_message(Duration::from_secs(3)).await });
    tokio::time::sleep(Duration::from_millis(100)).await;

    let other_work = tokio::time::timeout(Duration::from_secs(2), async {
        intercom.send("busy", message("unblocked")).await.unwrap();
        intercom.receive("busy").await.unwrap()
    })
    .await
    .expect("the shared connection must stay usable while a reader waits");

    assert_eq!(text_of(&other_work.expect("a message")), "unblocked");
    let _timed_out = waiter.await.unwrap();
    intercom.flush().await.unwrap();
}

#[tokio::test]
async fn a_bad_address_is_reported_rather_than_panicking() {
    let error = Intercom::connect("not-a-url", ChannelConfig::default())
        .await
        .expect_err("an unusable address must be an error");
    assert!(error.to_string().contains("not-a-url"));
}
