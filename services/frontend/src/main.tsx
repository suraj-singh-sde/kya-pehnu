import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { ApiError } from "@/api/client";
import { Layout } from "@/components/Layout";
import { initTheme } from "@/lib/theme";
import AddItems from "@/pages/AddItems";
import Chat from "@/pages/Chat";
import Debug from "@/pages/Debug";
import ItemDetail from "@/pages/ItemDetail";
import OutfitDetail from "@/pages/OutfitDetail";
import Settings from "@/pages/Settings";
import SignIn from "@/pages/SignIn";
import Setup from "@/pages/Setup";
import Wardrobe from "@/pages/Wardrobe";
import Welcome from "@/pages/Welcome";
import "./index.css";

initTheme();
// A missing item or a refused request will not fix itself: only retry when the server or the network failed.
const retry = (failures: number, error: unknown) => !(error instanceof ApiError && error.status < 500) && failures < 2;
const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 5_000, retry } } });

function NotFound() {
  return (
    <div className="lost">
      <h2>Nothing hangs here.</h2>
      <p>There is no page at this address.</p>
      <Link className="btn primary" to="/">
        Go to your wardrobe
      </Link>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="signin" element={<SignIn />} />
          <Route path="welcome" element={<Welcome />} />
          <Route element={<Layout />}>
            <Route index element={<Wardrobe />} />
            <Route path="items/:id" element={<ItemDetail />} />
            <Route path="outfits" element={<Navigate to="/chat" replace />} />
            <Route path="outfits/:id" element={<OutfitDetail />} />
            <Route path="chat" element={<Chat />} />
            <Route path="chat/:sessionId" element={<Chat />} />
            <Route path="add" element={<AddItems />} />
            <Route path="settings" element={<Settings />} />
            <Route path="setup" element={<Setup />} />
            <Route path="debug" element={<Debug />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
