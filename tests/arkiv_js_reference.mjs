// Prints the calldata the official JS SDK (@arkiv-network/sdk 1.0.x) builds for one createEntity call.
// The SDK runs its real createEntity; a fake transport answers the chain lookups and captures the
// signed transaction instead of sending it. Spec on stdin:
// {"schema": "canton-venues.log" | null, "attributes": [[name, tag, value, indexed], ...],
//  "lifetimeS": 86400, "readonly": false, "salt": "123"}
// A bytes value is hex, a u64 a decimal string.
import { createWalletClient, ExpirationTime } from "@arkiv-network/sdk"
import { rabdi } from "@arkiv-network/sdk/chains"
import { bytes, defineSchema, str, u64 } from "@arkiv-network/sdk/schema"
import { custom, hexToBytes, parseTransaction } from "viem"
import { privateKeyToAccount } from "viem/accounts"

const spec = JSON.parse(await new Promise((ok) => {
  let s = ""; process.stdin.on("data", (d) => (s += d)); process.stdin.on("end", () => ok(s))
}))
const columnOf = { str: () => str(), u64: () => u64(), bytes: () => bytes() }
const valueOf = { str: (v) => str(v), u64: (v) => u64(BigInt(v)), bytes: (v) => hexToBytes("0x" + v) }
const columns = Object.fromEntries(spec.attributes.map(([n, t, , indexed]) =>
  [n, indexed ? columnOf[t]().indexed() : columnOf[t]()]))
const values = Object.fromEntries(spec.attributes.map(([n, t, v]) => [n, valueOf[t](v)]))
const schema = spec.schema ? defineSchema(spec.schema, columns) : defineSchema(columns)

const chain = rabdi({ accessKey: "reference" })
let captured
const transport = custom({
  async request({ method, params }) {
    switch (method) {
      case "eth_chainId": return "0x" + chain.id.toString(16)
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
const client = createWalletClient({ chain, transport, account: privateKeyToAccount("0x" + "11".repeat(32)) })
try {
  await client.createEntity(schema, values, {
    expiration: ExpirationTime.fromSeconds(spec.lifetimeS),
    flags: { readonly: spec.readonly },
    salt: BigInt(spec.salt),
  })
} catch (e) {
  if (!captured) throw e
}
process.stdout.write(JSON.stringify({ to: captured.to, data: captured.data }))
