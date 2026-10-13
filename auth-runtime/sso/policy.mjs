// Native Logto policy: verified enterprise identities stay separate and responses are consumed once.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import saml from "samlify";

const require = createRequire(import.meta.url);
const samlPackage = JSON.parse(readFileSync(resolve(dirname(require.resolve("samlify")), "../package.json"), "utf8"));
if (samlPackage.version !== "2.13.1") throw new Error("Unsupported SSO validation dependency");

const requestModel = "OpenMontageSamlRequest";
const assertionModel = "OpenMontageSamlAssertion";
const digest = (value) => createHash("sha256").update(value).digest("hex");
const identifier = (value) => typeof value === "string" && value.length > 0 && value.length <= 2048;
const validBindings = (value) => value
  && ["audience", "recipient", "destination", "bearerRequestId", "responseRequestId"].every((key) => identifier(value[key]))
  && Number.isFinite(value.expiresAt) && value.expiresAt > Date.now();

// Samlify supplies the signature-verified assertion as the extractor's shortcut.
const extractFields = saml.Extractor.loginResponseFields;
saml.Extractor.loginResponseFields = (assertion) => [
  ...extractFields(assertion),
  { key: "openmontageAssertion", localPath: ["Assertion"], attributes: ["ID"], shortcut: assertion },
  { key: "openmontageConfirmation", localPath: ["Assertion", "Subject", "SubjectConfirmation"],
    attributes: [], context: true, shortcut: assertion },
  { key: "openmontageMethod", localPath: ["Assertion", "Subject", "SubjectConfirmation"],
    attributes: ["Method"], shortcut: assertion },
  { key: "openmontageBearer", localPath: ["Assertion", "Subject", "SubjectConfirmation", "SubjectConfirmationData"],
    attributes: ["Recipient", "InResponseTo", "NotOnOrAfter"], shortcut: assertion },
];

export async function registerSamlRequest(queries, jti, requestId, metadata) {
  if (!queries?.upsertInstance || !identifier(jti) || !identifier(requestId)) {
    throw new Error("Native SSO request could not be recorded");
  }
  await queries.upsertInstance({ modelName: requestModel, id: digest(jti),
    payload: { requestId, entityId: metadata.entityId, acsUrl: metadata.assertionConsumerServiceUrl },
    expiresAt: Date.now() + 600_000 });
}

export function verifiedSamlProfile(profile, extract) {
  const assertionId = extract.openmontageAssertion;
  const bearer = extract.openmontageBearer;
  const bindings = { audience: extract.audience, recipient: bearer?.recipient,
    bearerRequestId: bearer?.inResponseTo, destination: extract.response?.destination,
    responseRequestId: extract.response?.inResponseTo, expiresAt: Date.parse(bearer?.notOnOrAfter) };
  if (!identifier(assertionId) || !identifier(extract.issuer)
      || typeof extract.openmontageConfirmation !== "string"
      || extract.openmontageMethod !== "urn:oasis:names:tc:SAML:2.0:cm:bearer"
      || !validBindings(bindings)) {
    throw new Error("Native SSO response could not be verified");
  }
  return { ...profile, openmontageAssertionHash: digest(extract.issuer + "\0" + assertionId),
    openmontageBindings: bindings };
}

export function createSamlConsumption(pool, sql, table, fields) {
  return async (jti, assertionHash, bindings) => {
    if (!identifier(jti) || !/^[a-f0-9]{64}$/.test(assertionHash)
        || !validBindings(bindings)) {
      throw new Error("Native SSO response is unavailable");
    }
    const requestKey = digest(jti);
    const claimed = await pool.maybeOne(sql`
      WITH pending_request AS MATERIALIZED (
        SELECT * FROM ${table}
        WHERE ${fields.modelName}=${requestModel} AND ${fields.id}=${requestKey}
          AND ${fields.consumedAt} IS NULL AND ${fields.expiresAt}>clock_timestamp()
          AND ${fields.payload}->>'entityId'=${bindings.audience}
          AND ${fields.payload}->>'acsUrl'=${bindings.recipient}
          AND ${fields.payload}->>'acsUrl'=${bindings.destination}
          AND ${fields.payload}->>'requestId'=${bindings.bearerRequestId}
          AND ${fields.payload}->>'requestId'=${bindings.responseRequestId}
          AND to_timestamp(${bindings.expiresAt / 1000})>clock_timestamp()
        FOR UPDATE
      ), new_assertion AS (
        INSERT INTO ${table}
          (${fields.tenantId},${fields.modelName},${fields.id},${fields.payload},${fields.expiresAt})
        SELECT ${fields.tenantId},${assertionModel},${assertionHash},'{}'::jsonb,${fields.expiresAt}
        FROM pending_request
        ON CONFLICT (${fields.tenantId},${fields.modelName},${fields.id}) DO NOTHING
        RETURNING ${fields.id}
      ), consumed_request AS (
        UPDATE ${table} SET ${fields.consumedAt}=clock_timestamp()
        WHERE ${fields.tenantId}=(SELECT ${fields.tenantId} FROM pending_request)
          AND ${fields.modelName}=${requestModel} AND ${fields.id}=${requestKey}
          AND ${fields.consumedAt} IS NULL AND EXISTS(SELECT 1 FROM new_assertion)
        RETURNING ${fields.id}
      ) SELECT ${fields.id} FROM consumed_request
    `);
    if (!claimed) throw new Error("Native SSO response was already handled or expired");
  };
}

export async function consumeSamlResponse(queries, jti, profile) {
  if (!queries?.openmontageSamlConsume) throw new Error("Native SSO processing is unavailable");
  await queries.openmontageSamlConsume(jti, profile.openmontageAssertionHash, profile.openmontageBindings);
}
