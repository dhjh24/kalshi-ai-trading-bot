"use client";

import { useEffect, useState } from "react";
import { createStreamUrl } from "./api";

type TopicSubscriber = (payload: unknown) => void;

type TopicConnection = {
  source: EventSource;
  subscribers: Set<TopicSubscriber>;
};

const topicConnections = new Map<string, TopicConnection>();

function getTopicConnection(topic: string): TopicConnection {
  const existing = topicConnections.get(topic);
  if (existing) {
    return existing;
  }

  const source = new EventSource(createStreamUrl(topic));
  const subscribers = new Set<TopicSubscriber>();
  const connection = { source, subscribers };

  source.onmessage = (event) => {
    const envelope = JSON.parse(event.data) as {
      payload: unknown;
    };

    subscribers.forEach((subscriber) => {
      subscriber(envelope.payload);
    });
  };

  source.onerror = () => {
    if (subscribers.size === 0) {
      source.close();
      topicConnections.delete(topic);
    }
  };

  topicConnections.set(topic, connection);
  return connection;
}

export function useTopicStream<T>(
  topic: string,
  initialValue: T,
  projector?: (payload: unknown, previous: T) => T
) {
  const [value, setValue] = useState<T>(initialValue);

  useEffect(() => {
    const connection = getTopicConnection(topic);
    const subscriber = (payload: unknown) => {
      setValue((previous) =>
        projector ? projector(payload, previous) : (payload as T)
      );
    };

    connection.subscribers.add(subscriber);

    return () => {
      connection.subscribers.delete(subscriber);

      if (connection.subscribers.size === 0) {
        connection.source.close();
        topicConnections.delete(topic);
      }
    };
  }, [projector, topic]);

  return value;
}
