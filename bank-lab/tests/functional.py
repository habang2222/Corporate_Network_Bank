"""Probe bodies. Imported by the host runner, executed inside isolated clients."""

CUSTOMER = r'''
base = "http://web:8080"
alice, bob = login(base, "alice"), login(base, "bob")
check("customer login succeeds", 200, request(base, "/accounts", token=alice)[0])
check("incorrect credentials rejected", 401, request(base, "/login", "POST", {"username":"alice", "password":"intentionally-invalid-test-value"})[0])
check("unauthenticated account access rejected", 401, request(base, "/accounts")[0])
check("foreign account rejected", 403, request(base, "/accounts/A002", token=alice)[0])
status, accounts, _ = request(base, "/accounts", token=alice)
check("only customer's accounts listed", True, bool(accounts.get("accounts")) and all(a["customer_id"]=="c001" for a in accounts["accounts"]))
check("seeded customer account present", True, "A001" in [a["account_id"] for a in accounts.get("accounts",[])])
check("transaction history available", 200, request(base, "/accounts/A001/transactions", token=alice)[0])
before = [balance(base, alice, "A001"), balance(base, bob, "A002")]
payload = {"request_id": "test-" + uuid.uuid4().hex, "from_account":"A001", "to_account":"A002", "amount_cents":13}
status, value, _ = request(base, "/transfers", "POST", payload, alice)
check("transfer succeeds", 200, status)
check("transfer result committed", "completed", value.get("status"))
after = [balance(base, alice, "A001"), balance(base, bob, "A002")]
check("actual balances changed atomically", [before[0]-13, before[1]+13], after)
check("duplicate request returns original result", 200, request(base, "/transfers", "POST", payload, alice)[0])
check("duplicate does not debit twice", after, [balance(base, alice, "A001"), balance(base, bob, "A002")])
check("request ID payload conflict rejected", 409, request(base, "/transfers", "POST", dict(payload, amount_cents=14), alice)[0])
insufficient = dict(payload, request_id="test-"+uuid.uuid4().hex, amount_cents=after[0]+1)
check("insufficient funds rejected", 409, request(base, "/transfers", "POST", insufficient, alice)[0])
check("insufficient funds leave balances unchanged", after, [balance(base, alice, "A001"), balance(base, bob, "A002")])
status,history,_=request(base,"/accounts/A001/transactions",token=alice)
check("failed transfer retained in history",True,any(row["request_id"]==insufficient["request_id"] and row["status"]=="failed" for row in history.get("transactions",[])))
check("zero amount rejected", 422, request(base, "/transfers", "POST", dict(payload, request_id="test-"+uuid.uuid4().hex, amount_cents=0), alice)[0])
check("fractional amount rejected", 422, request(base, "/transfers", "POST", dict(payload, request_id="test-"+uuid.uuid4().hex, amount_cents=0.5), alice)[0])
check("foreign source account rejected", 403, request(base, "/transfers", "POST", dict(payload, request_id="test-"+uuid.uuid4().hex, from_account="A002",to_account="A001"), alice)[0])
before = [balance(base, alice, "A001"), balance(base, bob, "A002")]
jobs = []
for index in range(12):
    jobs.append((alice,{"request_id":"test-"+uuid.uuid4().hex,"from_account":"A001","to_account":"A002","amount_cents":2}))
    jobs.append((bob,{"request_id":"test-"+uuid.uuid4().hex,"from_account":"A002","to_account":"A001","amount_cents":1}))
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
    statuses = list(pool.map(lambda job: request(base,"/transfers","POST",job[1],job[0])[0], jobs))
check("concurrent transfers all commit", [200]*24, statuses)
after = [balance(base, alice, "A001"), balance(base, bob, "A002")]
check("concurrent transfer exact deltas", [before[0]-12,before[1]+12], after)
check("concurrent transfer balance conservation", sum(before), sum(after))
duplicate = {"request_id":"test-"+uuid.uuid4().hex,"from_account":"A001","to_account":"A002","amount_cents":7}
with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
    statuses = list(pool.map(lambda _: request(base,"/transfers","POST",duplicate,alice)[0], range(10)))
check("concurrent duplicate requests accepted", [200]*10, statuses)
check("concurrent duplicate applied exactly once", [after[0]-7,after[1]+7], [balance(base,alice,"A001"),balance(base,bob,"A002")])
check("customer logout succeeds", 200, request(base,"/logout","POST",{},alice)[0])
check("revoked session rejected", 401, request(base,"/accounts",token=alice)[0])
finish()
'''

EMPLOYEE = r'''
base="http://employee:8080"
teller,auditor,operator=login(base,"teller"),login(base,"auditor"),login(base,"operator")
check("teller can view synthetic customers",200,request(base,"/customers",token=teller)[0])
check("auditor cannot view customers",403,request(base,"/customers",token=auditor)[0])
check("operator cannot view customers",403,request(base,"/customers",token=operator)[0])
report_status,report_result,_=request(base,"/reports","POST",{},auditor)
check("auditor can create report",200,report_status)
check("generated report can be read",200,request(base,"/files/"+report_result["filename"],token=auditor)[0])
check("operator cannot create reports",403,request(base,"/reports","POST",{},operator)[0])
new_account={"account_id":"test_"+uuid.uuid4().hex,"customer_id":"c001"}
check("teller cannot create accounts",403,request(base,"/admin/accounts","POST",new_account,teller)[0])
status,created,_=request(base,"/admin/accounts","POST",new_account,operator)
check("operator creates synthetic account",200,status)
check("new synthetic account begins at zero",0,created.get("balance_cents"))
check("duplicate account creation rejected",409,request(base,"/admin/accounts","POST",new_account,operator)[0])
check("teller cannot execute operations",403,request(base,"/operations","POST",{"operation":"process"},teller)[0])
for operation in ["file-cycle","permission-denied","process","network"]:
    status,value,_=request(base,"/operations","POST",{"operation":operation},operator)
    check("operator fixed operation "+operation,200,status)
    check("operator operation expected OS outcomes "+operation,True,value.get("all_expected"))
check("arbitrary command rejected",422,request(base,"/operations","POST",{"operation":"sh -c id"},operator)[0])
filename="test-"+uuid.uuid4().hex+".txt"
check("employee file creation",200,request(base,"/files/"+filename,"PUT",{"content":"first version"},teller)[0])
check("employee file modification",200,request(base,"/files/"+filename,"PUT",{"content":"second version"},teller)[0])
check("employee can read own file",200,request(base,"/files/"+filename,token=teller)[0])
check("other employee cannot read file",404,request(base,"/files/"+filename,token=auditor)[0])
status=request(base,"/files/%2e%2e%2fservice-key",token=teller)[0]
check("path traversal rejected",[404,422],status,status in (404,422))
check("employee file deletion",200,request(base,"/files/"+filename,"DELETE",token=teller)[0])
check("deleted file absent",404,request(base,"/files/"+filename,token=teller)[0])
finish()
'''

EVENTS = r'''
base="http://events:8080"
for kind in ["file","process","permission","network","cpu","memory","disk"]:
    status,value,_=request(base,"/run","POST",{"kind":kind,"seconds":0.1,"memory_mib":2},internal=True)
    check("event worker "+kind+" request",200,status)
    check("event worker "+kind+" expected OS outcomes",True,value.get("success"))
    check("event worker "+kind+" subevents",True,bool(value.get("subevents")))
    if kind=="disk":
        encoded=json.dumps(value)
        check("bounded scratch disk reaches ENOSPC",True,"ENOSPC" in encoded or '"errno": 28' in encoded)
check("event arbitrary kind rejected",422,request(base,"/run","POST",{"kind":"shell","command":"id"},internal=True)[0])
finish()
'''
