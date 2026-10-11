import PocketBase from "pocketbase";

import type { TypedPocketBase } from "./pocketbase-types";
import { rememberRequestId } from './telemetry';

const configuredUrl = import.meta.env.VITE_POCKETBASE_URL?.trim();
const baseUrl = configuredUrl ? `${configuredUrl.replace(/\/+$/, '')}/` : '/';

export const pb = new PocketBase(baseUrl) as TypedPocketBase;

// Correlate failed SDK requests with backend logs without copying response data.
pb.afterSend = (response, data) => {
  rememberRequestId(data, response.headers.get('X-Request-ID'));
  return data;
};
