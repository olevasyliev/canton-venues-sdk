// Prints the calldata the official JS SDK (@arkiv-network/sdk) builds for one createEntity call.
// The SDK runs its real createEntity; a fake transport answers the chain lookups and captures the
// signed transaction instead of sending it. Spec on stdin:
// {"payloadHex": "...", "contentType": "...", "attributes": [[name, tag, value], ...],
//  "lifetimeS": 86400, "readonly": false, "salt": "123"}
import { createWalletClient, ExpirationTime, u64, str, bytes32 } from "@arkiv-network/sdk"
import { tiramisu } from "@arkiv-network/sdk/chains"
import { custom, parseTransaction, hexToBytes } from "viem"
import { privateKeyToAccount } from "viem/accounts"

const spec = JSON.parse(await new Promise((ok) => {
  let s = ""; process.stdin.on("data", (d) => (s += d)); process.stdin.on("end", () => ok(s))
}))
const tags = { u64: (v) => u64(BigInt(v)), str: (v) => str(v), bytes32: (v) => bytes32(v) }
const attributes = Object.fromEntries(spec.attributes.map(([n, t, v]) => [n, tags[t](v)]))

let captured
const transport = custom({
  async request({ method, params }) {
    switch (method) {
      case "eth_chainId": return "0x" + tiramisu.id.toString(16)
      case "eth_blockNumber": return "0x1000"
      case "eth_getTransactionCount": return "0x0"
      case "eth_estimateGas": return "0x100000"
      case "eth_gasPrice": return "0x3b9aca00"
      case "eth_maxPriorityFeePerGas": return "0x1"
      case "eth_getBlockByNumber": return { baseFeePerGas: "0x3b9aca00", number: "0x1000", timestamp: "0x0", transactions: [] }
      case "eth_sendRawTransaction": captured = parseTransaction(params[0]); throw new Error("captured")
      default: throw new Error("unexpected " + method)
    }
  },
})
const client = createWalletClient({
  chain: tiramisu, transport,
  account: privateKeyToAccount("0x" + "11".repeat(32)),
})
try {
  await client.createEntity({
    payload: hexToBytes("0x" + spec.payloadHex),
    contentType: spec.contentType,
    attributes,
    expires: ExpirationTime.fromSeconds(spec.lifetimeS),
    flags: { readonly: spec.readonly },
    salt: BigInt(spec.salt),
  })
} catch (e) {
  if (!captured) throw e
}
process.stdout.write(JSON.stringify({ to: captured.to, data: captured.data }))
