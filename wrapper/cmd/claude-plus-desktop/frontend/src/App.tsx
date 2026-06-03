import Terminal from "./Terminal";
import Sessions from "./Sessions";
import Stream from "./Stream";
import StatusBar from "./StatusBar";

function App() {
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", margin: 0 }}>
      <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
        <Sessions />
        <main style={{ flex: 1, minWidth: 0, background: "#1e1e1e" }}>
          <Terminal />
        </main>
        <Stream />
      </div>
      <StatusBar />
    </div>
  );
}

export default App;
