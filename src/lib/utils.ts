import { type ClassValue, clsx } from "clsx"
import { twMerge } from "tailwind-merge"
import { pb } from "./pocketbase";
import { toast } from "sonner";
import {  type UsersResponse } from "./pocketbase-types";
import { errorMessage, isCancelled } from './errors';
import { reportError } from './telemetry';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function handleError(error: unknown) {
  if (isCancelled(error)) return;
  reportError(error, 'request');
  toast.error("An error occurred", {
    description: errorMessage(error),
    richColors: true
  })
}

export function getUserRecord(): UsersResponse | null {
  return pb.authStore.record as UsersResponse | null;
}
