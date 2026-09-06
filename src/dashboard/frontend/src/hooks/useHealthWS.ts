import { useEffect, useState } from "react";
import { healthWS } from "../services/websocket";

export function useHealthWS() {
  const [data, setData] = useState<unknown>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    healthWS.connect();
    setConnected(true);
    const unsub = healthWS.subscribe(setData);
    return () => {
      unsub();
      healthWS.disconnect();
      setConnected(false);
    };
  }, []);

  return { data, connected };
}
