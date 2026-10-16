import { createContext, useContext } from "react";

export type Me = {
  user: { name: string; role: string; email: string; crm_user_id: string | null };
  tenant: { tenant_id: string; name: string; csm_weekly_capacity: number };
  csms: { crm_user_id: string; name: string }[];
};
export const MeCtx = createContext<Me | null>(null);
export const useMe = () => useContext(MeCtx)!;
