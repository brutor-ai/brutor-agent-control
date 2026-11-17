/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Response/request types derived from the Control Plane's OpenAPI document.
 *
 * Why derive instead of hand-write
 * --------------------------------
 * `GatewayApiClient` declares ~117 of its methods as `Promise<any>`. Replacing
 * those with hand-written interfaces would be an *assertion* about what the
 * server sends, and nothing verifies it — TypeScript cannot check a type
 * against a wire payload, so a wrong field name or a wrong optionality
 * compiles cleanly and fails at runtime. Worse, once a field is typed
 * non-optional the defensive `data?.x ?? fallback` guards around it start
 * looking redundant and get removed.
 *
 * These aliases instead read the types straight out of
 * `src/lib/generated/control-plane-api.d.ts`, which `npm run
 * gen:api-types:control` produces from the FastAPI/Pydantic response models.
 * A rename on the server changes the generated type, and every call site that
 * relied on the old shape stops compiling.
 *
 * Which document: the Control Plane
 * has one OpenAPI document per edition, and this file reads the types of the
 * COMMUNITY one. A path only the Enterprise Control Plane serves is not a
 * key of `paths` here, so a Community module that names one does not
 * compile. The Enterprise package has its own generated file and its own
 * aliases over it (`enterprise/src/lib/api/controlPlaneTypes.ts`), built
 * from the `…Of` types below.
 *
 * Regenerate after any Control Plane schema change:
 *   npm run gen:api-types:control
 *
 * Not every method of the client returns one of these. A method returns
 * `Promise<any>` where the Control Plane serves the route without a
 * `response_model`, or with one whose payload is a free dictionary: the
 * generated type then has no fields. The count of such methods is held by
 * `src/lib/__tests__/apiTypingRatchet.test.ts` and only goes down. A new
 * method is typed with the `ApiResponse` alias of its own edition.
 *
 * Usage:
 *   async listComplianceFrameworks(): Promise<ApiResponse<'/v1/admin/compliance/frameworks', 'get'>> { ... }
 */

import type { paths } from '../generated/control-plane-api';

type JsonContent<T> = T extends { content: { 'application/json': infer R } } ? R : never;

/**
 * The success response body for one operation of a generated `paths` type.
 *
 * Checks 200 then 201 — FastAPI returns 201 from most creates, and looking
 * only at 200 silently resolves those to `never`, which shows up as
 * "Property 'x' does not exist on type 'never'" at the call site. 204 maps to
 * `void` since there is no body to describe.
 *
 * The body is `Answered`: every field of it is there, a defaulted one too.
 *
 * Still resolves to `never` for operations with no JSON success response (a
 * binary download, say). That is deliberate: `Promise<never>` is a loud
 * compile error rather than a silent `any`.
 */
export type ApiResponseOf<
  Paths,
  P extends keyof Paths,
  M extends keyof Paths[P],
> = Paths[P][M] extends { responses: infer R }
  ? R extends { 200: infer Ok }
    ? Answered<JsonContent<Ok>>
    : R extends { 201: infer Created }
      ? Answered<JsonContent<Created>>
      : R extends { 204: unknown }
        ? void
        : never
  : never;

/**
 * A response model as the Control Plane answers with it.
 *
 * The types are generated with `--default-non-nullable=false`: a field with
 * a default is optional, because a request may leave it out. In an answer
 * the Control Plane fills in every default, so such a field is always there.
 * `Answered` makes every field of the model required, in every object it
 * models and in the items of its lists; `null` stays where the contract
 * allows it. A dictionary of a modelled type has each value answered; a
 * free dictionary is left as it is.
 */
export type Answered<T> = T extends readonly (infer Item)[]
    ? Answered<Item>[]
    : T extends object
      ? string extends keyof T
          ? T extends { [key: string]: infer Value }
              ? unknown extends Value
                  ? T
                  : { [key: string]: Answered<Value> }
              : T
          : { [K in keyof T]-?: Answered<Exclude<T[K], undefined>> }
      : T;

/** The JSON request body for one operation of a generated `paths` type. */
export type ApiRequestBodyOf<
  Paths,
  P extends keyof Paths,
  M extends keyof Paths[P],
> = Paths[P][M] extends { requestBody?: infer B } ? JsonContent<NonNullable<B>> : never;

/** Every key of `T`: of each member, when `T` is a union. */
type KeysOf<T> = T extends unknown ? keyof T : never;

/**
 * What a value may be where the contract types a field of a body `T`.
 *
 * The contract's type, with two allowances. A free dictionary
 * (`{ [key: string]: unknown }`, the Control Plane's `Dict[str, Any]`) takes
 * any object, an interface included (an interface has no index signature, so
 * it is not assignable to the dictionary type itself). And inside an object
 * the Control Plane models, a field may be left out: an update names the
 * fields it changes, and a nested object the contract requires a field of is
 * held to that by the Control Plane's 422.
 */
type Sendable<T> = T extends readonly (infer Item)[]
    ? Sendable<Item>[]
    : T extends object
      ? string extends keyof T
          ? T extends { [key: string]: infer Value }
              ? unknown extends Value
                  ? object
                  : { [key: string]: Sendable<Value> }
              : object
          : { [K in keyof T]?: Sendable<T[K]> }
      : T;

/**
 * The body of an update as it may be sent: the contract's `Body`, each field
 * held to what `Sendable` allows for it and none of them required (an update
 * names the fields it changes; a field the Control Plane does require and
 * does not get is its 422).
 */
export type SendableBody<Body> = { [K in keyof Body]?: Sendable<Body[K]> };

/** The members of `T` that the contract models: objects with named keys, and lists. Not a free dictionary, not a scalar. */
type Modelled<T> = T extends readonly unknown[] ? T : T extends object ? (string extends keyof T ? never : T) : never;

/** The type the contract gives key `K`, in whichever member of `T` has it. */
type ValueAt<T, K extends PropertyKey> = T extends unknown ? (K extends keyof T ? T[K] : never) : never;

/**
 * `Sent`, with whatever the contract's `Body` does not allow at any depth
 * typed `never`.
 *
 * Where the contract models an object, each key of what is sent has to be
 * one of its keys, and so on down through objects and the items of lists.
 * Where it models one and what is sent is `any` or a free dictionary, the
 * whole value is `never`: either could hold any key. Where the contract
 * itself has a scalar or a free dictionary there is nothing to hold, and
 * `Sent` stands as it is.
 */
type ExactAt<Body, Sent> = [Modelled<NonNullable<Body>>] extends [never]
    ? Sent
    : 0 extends 1 & Sent
      ? never
      : Sent extends readonly (infer Item)[]
        ? Modelled<NonNullable<Body>> extends readonly (infer BodyItem)[]
            ? readonly ExactAt<BodyItem, Item>[]
            : Sent
        : Sent extends object
          ? string extends keyof Sent
              ? never
              : { [K in keyof Sent]: K extends KeysOf<Modelled<NonNullable<Body>>> ? ExactAt<ValueAt<Modelled<NonNullable<Body>>, K>, Sent[K]> : never }
          : Sent;

/**
 * A request body that names no key its contract type lacks, at any depth.
 *
 * `Sent` is the type of the value passed, inferred at the call. The result
 * is that type, held to `SendableBody<Body>`, with every key `Body` does
 * not name typed `never`: at the top (of any member, when the value is one
 * of several shapes) and below it, in every object and every item of a
 * list the contract models (`ExactAt`). Such a key does not compile,
 * whether the value is written in place or held in a variable of a wider
 * type. (An assignment to `Body` alone refuses an unknown key only in an
 * object written in place.) The Control Plane refuses an unknown field in
 * the body of an update, so the refusal is here, at the type check.
 *
 * A value typed `any` could hold anything, and so could a free dictionary
 * (`Record<string, unknown>`): both are refused (`never`), as the body and
 * wherever the contract models an object. The body is built with a type
 * that names its keys.
 *
 * What the compiler cannot hold (`src/lib/api/bodyExactness.ts` has each
 * case): a free dictionary SPREAD into an object literal, because the type
 * of the literal does not carry the dictionary's keys; a value asserted to
 * another type (`as`); and whatever is inside a field the contract itself
 * types as a free dictionary.
 */
export type BodyExactly<Body, Sent> = 0 extends 1 & Sent
    ? never
    : string extends KeysOf<Sent>
      ? never
      : Sent & ExactAt<Body, Sent> & SendableBody<Body> & { [K in Exclude<KeysOf<Sent>, KeysOf<NonNullable<Body>>>]?: never };

/** The query-string parameters for one operation of a generated `paths` type. */
export type ApiQueryOf<
  Paths,
  P extends keyof Paths,
  M extends keyof Paths[P],
> = Paths[P][M] extends { parameters: { query?: infer Q } } ? Q : never;

/** Every path string the Community Control Plane serves. */
export type ApiPath = keyof paths;

/** The success response body for one operation of the Community Control Plane. */
export type ApiResponse<P extends ApiPath, M extends keyof paths[P]> = ApiResponseOf<paths, P, M>;

/** The JSON request body for one operation of the Community Control Plane. */
export type ApiRequestBody<P extends ApiPath, M extends keyof paths[P]> = ApiRequestBodyOf<paths, P, M>;

/** The query-string parameters for one operation of the Community Control Plane. */
export type ApiQuery<P extends ApiPath, M extends keyof paths[P]> = ApiQueryOf<paths, P, M>;

/**
 * The JSON request body of one operation of the Community Control Plane, and
 * no key beside its own (`BodyExactly`). The parameter type of a client
 * method that sends an update:
 *
 *   async updateThing<Sent>(id: string, body: ApiBodyExactly<'/v1/admin/things/{thing_id}', 'patch', Sent>)
 *
 * `src/lib/__tests__/updateBodyTyping.test.ts` holds every PATCH and PUT of
 * the console to it.
 */
export type ApiBodyExactly<P extends ApiPath, M extends keyof paths[P], Sent> = BodyExactly<ApiRequestBody<P, M>, Sent>;

/**
 * The JSON body of one update of the Community Control Plane as it may be
 * sent (`SendableBody`): the type to build an update's body with, and to
 * give a form's save callback.
 */
export type ApiSendableBody<P extends ApiPath, M extends keyof paths[P]> = SendableBody<ApiRequestBody<P, M>>;
