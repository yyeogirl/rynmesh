import { useOutletContext } from "react-router-dom";
import type { NodeClient } from "./domain/nodeClient";
import type {
  ConfirmRequest,
  FirstSuccessStatus,
  NodeStatus,
  Peer,
  RegistryStatus,
  ToastMessage,
} from "./domain/types";

export interface AppOutletContext {
  client: NodeClient;
  node: NodeStatus;
  registry: RegistryStatus;
  peers: Peer[];
  firstSuccess?: FirstSuccessStatus | null;
  openFirstSuccess?: () => void;
  refreshShell: () => Promise<void>;
  refreshFirstSuccess?: () => Promise<FirstSuccessStatus>;
  confirm: (request: ConfirmRequest) => void;
  notify: (tone: ToastMessage["tone"], text: string) => void;
}

export function useAppContext() {
  return useOutletContext<AppOutletContext>();
}
