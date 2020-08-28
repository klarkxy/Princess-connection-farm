"""
新的启动函数，支持Batch，schedule操作等。
"""
import multiprocessing
import time
import traceback
from multiprocessing import Process
from multiprocessing.managers import SyncManager
from queue import PriorityQueue
from typing import List, Tuple, Optional, Dict

import adbutils
import keyboard

from automator_mixins._base import Multithreading
from core import log_handler
from core.Automator import Automator
from core.constant import USER_DEFAULT_DICT as UDD
from core.usercentre import AutomatorRecorder, parse_batch
from core.utils import diffday, PrintToStr
from emulator_port import *
from pcr_config import enable_auto_find_emulator, emulator_ports, selected_emulator, max_reboot, \
    trace_exception_for_debug, s_sckey, s_sentstate

acclog = log_handler.pcr_acc_log()

def _connect():  # 连接adb与uiautomator
    try:
        if enable_auto_find_emulator:
            port_list = check_known_emulators()
            print("自动搜寻模拟器：" + str(port_list))
            for port in port_list:
                os.system('cd adb & adb connect ' + emulator_ip + ':' + str(port))
        if len(emulator_ports) != 0:
            for port in emulator_ports:
                os.system('cd adb & adb connect ' + emulator_ip + ':' + str(port))
        # os.system 函数正常情况下返回是进程退出码，0为正常退出码，其余为异常
        if os.system('cd adb & adb connect ' + selected_emulator) != 0:
            pcr_log('admin').write_log(level='error', message="连接模拟器失败")
            exit(1)
    except Exception as e:
        pcr_log('admin').write_log(level='error', message='连接失败, 原因: {}'.format(e))
        exit(1)


# https://blog.csdn.net/qq_45587822/article/details/105950260
class MyManager(SyncManager):
    pass


class MyPriorityQueue(PriorityQueue):
    def get_attribute(self, name):
        return getattr(self, name)


def _get_manager():
    MyManager.register("PriorityQueue", MyPriorityQueue)
    m = MyManager()
    m.start()
    return m

def time_period_format(tm) -> str:
    tm = int(tm)
    if tm < 60:
        return f"{tm}s"
    elif tm < 3600:
        return f"{tm // 60}m {tm % 60}s"
    elif tm < 3600 * 24:
        return f"{tm // 3600}h {(tm % 3600) // 60}m {tm % 60}s"
    else:
        return f"{tm // (3600 * 24)}d {(tm % (3600 * 24)) // 3600}h {(tm % 3600) // 60}m {tm % 60}s"
class Device:
    """
    设备类，存储设备状态等。
    之后可以扩充雷电的开关操作
    """
    # 设备状态
    DEVICE_OFFLINE = 0  # 离线
    DEVICE_AVAILABLE = 1  # 可用
    DEVICE_BUSY = 2  # 正忙

    def __init__(self, d: adbutils.AdbDevice):
        self.serial = d.serial
        self.d = d
        self.state = 0
        self._in_process = False  # 是否已经进入多进程
        self.cur_acc = ""  # 当前正在处理的账号
        self.cur_rec = ""  # 当前正在处理的存档目录
        self.time_wake = 0  # 上次开机时间
        self.time_busy = 0  # 上次忙碌时间

    def init(self):
        self.state = self.DEVICE_AVAILABLE
        self.time_busy = 0
        self.time_wake = time.time()

    def start(self):
        self.state = self.DEVICE_BUSY
        self.time_busy = time.time()

    def register(self, acc="", rec=""):
        self.cur_acc = acc
        self.cur_rec = rec

    def stop(self):
        self.state = self.DEVICE_AVAILABLE
        self.time_busy = 0
        self.cur_acc = ""
        self.cur_rec = ""

    def offline(self):
        self.state = self.DEVICE_OFFLINE
        self.time_wake = 0

    def in_process(self):
        self._in_process = True

    def out_process(self):
        self._in_process = False

    @staticmethod
    def device_is_connected(d: adbutils.AdbDevice):
        try:
            d.say_hello()
            s = d.shell("dumpsys activity | grep mResume", timeout=5)
            if "Error" in s:
                # adb 崩坏
                return False
        except adbutils.errors.AdbError:
            return False
        else:
            return True

    def is_connected(self):
        return self.device_is_connected(self.d)


class AllDevices:
    """
    全部设备控制类，包含了包括connect在内的一些操作。
    """

    def __init__(self, device_type="雷电"):
        self.device_type = device_type
        self.devices: Dict[str, Device] = {}  # serial : device

    def add_device(self, d: adbutils.AdbDevice):
        """
        添加一个设备，若该设备不存在，则添加；若该设备的状态为offline但已连接，更新状态为available
        """
        s = d.serial
        if Device.device_is_connected(d):
            if s in self.devices:
                if self.devices[s].state == Device.DEVICE_OFFLINE:
                    self.devices[s].init()
                    return True
                else:
                    return False
            else:
                self.devices[s] = Device(d)
                self.devices[s].init()
                return True
        else:
            if s in self.devices:
                if self.devices[s].state != Device.DEVICE_OFFLINE:
                    self.devices[s].offline()
            else:
                self.devices[s] = Device(d)
                self.devices[s].offline()

    def connect(self):
        _connect()
        dl = adbutils.adb.device_list()
        t = False
        for d in dl:
            if self.add_device(d):
                t = True
        return t

    def process_method(self, device_message: dict):
        serial = device_message["serial"]
        method = device_message["method"]
        device = self.devices[serial]
        if type(method) is str:
            device.__getattribute__(method)()
        elif type(method) is tuple:
            device.__getattribute__(method[0])(*method[1:])

    def get(self):
        """
        获取一个空闲的设备，若获取失败，返回None
        若获取成功，返回device serial，并且该设备被标记为busy
        """
        for s, d in self.devices.items():
            if d.state == Device.DEVICE_AVAILABLE:
                d.start()
                return s
        return None

    def full(self):
        """
        判断是否所有设备均空闲
        """
        for d in self.devices.values():
            if d.state == Device.DEVICE_BUSY:
                return False
        return True

    def put(self, s):
        """
        放回一个用完的设备，更新该设备状态
        :param s: 设备的Serial
        """
        if self.devices[s].is_connected():
            self.devices[s].stop()
        else:
            self.devices[s].offline()

    def count(self):
        """
        返回当前busy状态的设备数
        """
        cnt = 0
        for i in self.devices.values():
            if i.state == Device.DEVICE_BUSY:
                cnt += 1
        return cnt

    def count_processed(self):
        """
        返回当前_in_process的设备总数
        """
        cnt = 0
        for i in self.devices.values():
            if i._in_process:
                cnt += 1
        return cnt

    def list_all_free_devices(self) -> List[Device]:
        """
        返回当前全部空闲的设备
        """
        L = []
        for i in self.devices.values():
            if i.state == Device.DEVICE_AVAILABLE:
                L += [i]
        return L

    def show(self):
        """
        显示当前全部设备状态
        """
        print("= 设备信息 =")
        for i, j in self.devices.items():
            print(i, ": ", end="")
            if j.state == Device.DEVICE_OFFLINE:
                print("离线")
            elif j.state == Device.DEVICE_AVAILABLE:
                print("空闲", " 开机时间", time_period_format(time.time() - j.time_wake))
            elif j.state == Device.DEVICE_BUSY:
                tm = time.time()
                print("正忙", " 开机时间", time_period_format(tm - j.time_wake), " 本次工作时间",
                      time_period_format(tm - j.time_busy), end="")
                if j.cur_acc != "":
                    print(" 当前任务：账号", j.cur_acc, AutomatorRecorder.get_user_state(j.cur_acc, j.cur_rec), end="")
                print()


class PCRInitializer:
    """
    PCR启动器，包含进程池逻辑、任务调度方法等。
    """

    def __init__(self, emulator="雷电"):
        """
        self.available_devices：multiprocessing.queue类型
            用于多进程，存放当前已连接但是空闲中的设备。queue[str]
        self.devices_state：字典类型，存储设备信息。
            {device_str : state_dict}
        self.tasks：queue.PriorityQueue类型，按优先级从高到低排序一系列任务
        """
        self.devices = AllDevices(emulator)
        self.mgr = _get_manager()
        self.tasks: MyPriorityQueue = self.mgr.__getattribute__("PriorityQueue")()  # 优先级队列
        self.out_queue: multiprocessing.Queue = self.mgr.Queue()  # 外部接收信息的队列
        self.listening = False  # 侦听线程是否开启
        self.finished_tasks = []  # 已经完成的任务
        self.log_queue = queue.Queue()  # 消息队列

    def connect(self):
        """
        连接设备，初始化设备
        """
        t = self.devices.connect()
        if not t:
            return
        if os.system('python -m uiautomator2 init') != 0:
            pcr_log('admin').write_log(level='error', message="初始化 uiautomator2 失败")
            exit(1)

    def write_log(self, msg):
        self.log_queue.put(msg)

    def get_log(self):
        try:
            return self.log_queue.get(block=False)
        except queue.Empty:
            return None

    def _add_task(self, task):
        """
        队列中添加任务五元组
        """
        rs = AutomatorRecorder(task[1], task[4]).get_run_status()
        if task[3] and rs["finished"]:
            if task not in self.finished_tasks:
                self.finished_tasks += [task]
        else:
            self.tasks.put(task)

    def add_task(self, task: Tuple[int, str, dict], continue_, rec_addr):
        """
        向优先级队列中增加一个task
        该task为四元组，(priority, account, task, continue_, rec_addr)
        """
        task = (0 - task[0], task[1], task[2], continue_, rec_addr)  # 最大优先队列
        self._add_task(task)

    def add_tasks(self, tasks: list, continue_, rec_addr):
        """
        向优先级队列中增加一系列tasks
        该tasks为一个列表类型，每个元素为四元组，(priority, account, task, continue_, rec_addr)
        """
        for task in tasks:
            self.add_task(task, continue_, rec_addr)

    def clear_tasks(self):
        """
        清空任务队列
        """
        while not self.tasks.empty():
            try:
                self.tasks.get(False)
            except queue.Empty:
                continue
            self.tasks.task_done()

    @staticmethod
    def run_task(device, account, task, continue_, rec_addr):
        """
        让device执行任务：account做task
        :param device: 设备名
        :param account:  账户名
        :param task:  任务名
        :param continue_:  是否继续上次中断的位置
        :param rec_addr: 进度保存目录
        :return 是否成功执行
        """

        a: Optional[Automator] = None
        try:
            keyboard.release('p')
            Multithreading({}).state_sent_resume()
            a = Automator(device)
            a.init_account(account, rec_addr)
            a.start()
            user = a.AR.getuser()
            account = user["account"]
            password = user["password"]
            a.log.write_log("info", f"即将登陆： 用户名 {account}")  # 显然不需要输出密码啊喂！
            a.start_th()
            a.start_async()
            a.start_shuatu()
            a.login_auth(account, password)
            acclog.Account_Login(account)
            out = a.RunTasks(task, continue_, max_reboot, rec_addr=rec_addr)
            if out:
                a.change_acc()
            acclog.Account_Logout(account)
            return out
        except Exception as e:
            pcr_log(account).write_log('error', message=f'initialize-检测出异常：{type(e)} {e}')
            if trace_exception_for_debug:
                tb = traceback.format_exc()
                pcr_log(account).write_log('error', message=tb)
            try:
                a.fix_reboot(False)
                return False
            except Exception as e:
                pcr_log(account).write_log('error', message=f'initialize-自动重启失败：{type(e)} {e}')
                if trace_exception_for_debug:
                    tb = traceback.format_exc()
                    pcr_log(account).write_log('error', message=tb)
                return False
        finally:
            if a is not None:
                a.stop_th()

    @staticmethod
    def _do_process(device: Device, queue, out_queue):
        """
        执行run_task的消费者进程
        device: 传入的设备信息
        queue：  任务优先级队列
        out_queue： 向外消息传递的队列
        """
        serial = device.serial
        while True:
            _task = queue.get()
            if _task == (-99999999, None, None, None, None):
                break
            priority, account, task, continue_, rec_addr = _task
            out_queue.put({"task": {"statue": "start", "task": _task, "device": serial}})
            out_queue.put({"device": {"serial": serial, "method": "start"}})
            out_queue.put({"device": {"serial": serial, "method": ("register", account, rec_addr)}})
            res = PCRInitializer.run_task(serial, account, task, continue_, rec_addr)
            if not res:
                out_queue.put({"task": {"statue": "fail", "task": _task, "device": serial}})
            else:
                out_queue.put({"task": {"statue": "success", "task": _task, "device": serial}})
            if not res and not device.is_connected():
                # 可能模拟器断开
                out_queue.put({"device": {"serial": serial, "method": "offline"}})
                queue.put(_task)
            else:
                out_queue.put({"device": {"serial": serial, "method": "stop"}})
        out_queue.put({"device": {"serial": serial, "method": "out_process"}})

    def process_task_method(self, msg):
        priority, account, task, continue_, rec_addr = msg["task"]
        if msg["statue"] in ["fail", "success"]:
            self.finished_tasks += [msg["task"]]
            if msg["statue"] == "fail":
                self.write_log(
                    f"账号{account}执行失败！设备：{msg['device']} 状态：{AutomatorRecorder.get_user_state(account, rec_addr)}")
            else:
                self.write_log(f"账号{account}执行成功！")
        elif msg["statue"] == "start":
            self.write_log(f"账号{account}开始执行，设备：{msg['device']} 进度存储目录 {rec_addr}")

    def _listener(self):
        """
        侦听线程，获取子进程从out_queue中返回的消息
        """
        self.listening = True
        while True:
            msg = self.out_queue.get()
            if msg is None:
                break
            if "device" in msg:
                self.devices.process_method(msg["device"])
            if "task" in msg:
                self.process_task_method(msg["task"])
        self.listening = False

    def start(self):
        """
        进入分配任务给空闲设备的循环
        """
        device_list = self.devices.list_all_free_devices()
        if not self.listening:
            # 打开侦听线程
            threading.Thread(target=PCRInitializer._listener, args=(self,), daemon=True).start()
        while self.listening == 0:
            pass
        for d in device_list:
            if not d._in_process:
                d._in_process = True
                Process(target=PCRInitializer._do_process,
                        kwargs=dict(device=d, queue=self.tasks, out_queue=self.out_queue), daemon=True).start()

    def stop(self, join=False, clear=False):
        if clear:
            self.clear_tasks()
        for _ in range(self.devices.count_processed()):
            self.tasks.put((-99999999, None, None, None, None))
        if join:
            while not self.devices.full():
                time.sleep(1)
        # 侦听线程不能结束，要持续接收可能出现的method信息

    def join(self):
        """
        等待任务队列中所有任务全部执行完毕且所有device空闲
        """
        self.stop(join=True, clear=False)

    def get_status(self):
        """
        获取队列中序号、账号、执行目录、当前状态
        """
        q = self.tasks.get_attribute("queue")
        L = []
        for ind, T in enumerate(q):
            if type(T) is not tuple or len(T) is not 5:
                print("DEBUG: ", T)
                break
            (_, acc, _, _, rec) = T
            state = AutomatorRecorder.get_user_state(acc, rec)
            L += [(ind, acc, rec, state)]
        return L

    def is_batch_running(self, batch) -> bool:
        """
        检测某一个batch是否在执行中
        :param batch:
        """
        q = self.tasks.get_attribute("queue")
        for _, _, _, _, rec in q:
            a, b = os.path.split(rec)
            if a == '':
                continue
            if b == batch:
                return True
        return False

    def show(self):
        """
        显示当前队列中的任务
        """
        L = self.get_status()
        print("↑↑ 任务等待队列")
        for ind, acc, rec, _ in L:
            print(f"<{ind}> 账号：{acc}  执行目录：{rec}")


class Schedule:
    """
    Schedule控制器：控制向PCRInitializer中定时添加task。
    """

    def __init__(self, name: str, pcr: Optional[PCRInitializer]):
        self.name = name
        self.schedule = AutomatorRecorder.getschedule(name)
        self.pcr = pcr
        self.state = 0
        self.config = {}
        self.SL = []  # 处理后的schedule
        self.run_status = {}  # 运行状态
        self.checked_status = {}  # 存放一个计划是否已经被add过
        self.subs = {}  # 关系表
        self.not_restart_name = []  # record=1，不用重启的列表
        self.always_restart_name = []  # record=2，循环执行的列表
        self._parse()
        self._init_status()
        self.run_thread: Optional[threading.Thread] = None

    def _parse(self):
        """
        将schedule进一步处理为适合读取的形式 -> List[
            (type,name,batch,condition,rec_addr)
        ]与self.config
        其中，将batchlist解析为新condition：前置batch已经完成。
        此外，构造关系表：self.subs={
            "name":("batch","rec_addr")
             or "name":[("batch1","rec_addr1"),("batch2","rec_addr2"),...]
        }
        """
        for s in self.schedule["schedules"]:
            if s["type"] == "config":
                self.config.update(s)
                continue
            typ = s["type"]
            nam = s["name"]
            cond = s["condition"]
            rectype = s["record"]
            if rectype == 1:
                self.not_restart_name += [nam]
            if "batchfile" in s:
                rec_addr = os.path.join("rec", self.name, s["name"], s["batchfile"])
                self.SL += [(typ, nam, s["batchfile"], cond, rec_addr)]
                self.subs[nam] = (s["batchfile"], rec_addr)
                if rectype == 2:
                    self.always_restart_name += [(nam, s["batchfile"])]
            elif "batchlist" in s:
                b0 = s["batchlist"][0]
                rec_addr = os.path.join("rec", self.name, s["name"], b0)
                self.SL += [(typ, nam, b0, cond, rec_addr)]
                self.subs[nam] = [(b0, rec_addr)]
                for b in s["batchlist"][1:]:
                    cond = {}
                    cond["_last_rec"] = rec_addr  # 完成的batch会在rec_addr中留下一个_fin文件用于检测。
                    rec_addr = os.path.join("rec", self.name, s["name"], b)
                    self.SL += [("wait", nam, b, cond, rec_addr)]  # 后续任务均为wait（等待前一batch完成）
                    self.subs[nam] += [(b, rec_addr)]
                if rectype == 2:
                    self.always_restart_name += [(nam, s["batchlist"][-1])]

    @staticmethod
    def _default_state():
        return {}

    def _save(self, obj):
        """
        将自身的状态存储至rec/<schedule_name>/state.txt
        """
        os.makedirs(os.path.join("rec", self.name), exist_ok=True)
        with open(os.path.join("rec", self.name, "state.txt"), "w") as f:
            json.dump(obj, f)

    def _load(self) -> dict:
        """
        获取自身的状态
        """
        os.makedirs(os.path.join("rec", self.name), exist_ok=True)
        target = os.path.join("rec", self.name, "state.txt")
        try:
            f = open(target, "r")
            js = json.load(f)
            f.close()
            return js
        except:
            self._save(self._default_state())
            return self._default_state()

    def reload(self):
        """
        已经完成的任务再次加入队列中。
        :return:
        """
        Q = self.pcr.tasks.get_attribute("queue")
        L = []
        for i in self.pcr.finished_tasks:
            if i not in Q:
                L += [i]
        self.pcr.finished_tasks.clear()
        for i in L:
            self.pcr._add_task(i)

    def restart(self, name=None):
        """
        重新开始某一个schedule，
        name设置为None时，全部重新开始
        """
        self._init_status()
        self._set_users(name, 2)
        self.reload()

    def del_file_in_path(self, path):
        for i in os.listdir(path):
            path_file = os.path.join(path, i)
            if os.path.isfile(path_file):
                try:
                    os.remove(path_file)
                except Exception as e:
                    self.log('error', f'删除记录文件出现错误：{e}')
            else:
                self.del_file_in_path(path_file)

    def _set_users(self, name, mode):
        """
        统一设置run_status。
        mode = 0：完成并清除Error
        mode = 1：清除Error
        mode = 2：重置
        """
        for _, nam, b, _, rec in self.SL:
            if nam == name or name is None:
                parsed = parse_batch(AutomatorRecorder.getbatch(b))
                for _, acc, _ in parsed:
                    AR = AutomatorRecorder(acc, rec)
                    rs = AR.get_run_status()
                    if mode == 0:
                        rs["finished"] = True
                        rs["error"] = None
                    if mode == 1:
                        if rs["error"] is not None:
                            rs["error"] = None
                            rs["finished"] = False
                    if mode == 2:
                        if name is None and nam in self.not_restart_name:
                            continue
                        if name is None or name == nam:
                            if os.path.isdir(rec):
                                self.del_file_in_path(rec)
                            if rs["error"] is None:
                                rs["finished"] = False
                                rs["current"] = "..."
                    AR.set_run_status(rs)

    def clear_error(self, name=None):
        """
        清除某一个schedule的错误
        name设置为None时，清除全部错误
        """
        self._set_users(name, 1)

    def finish_schedule(self, name=None):
        """
        完成某一个schedule的内容
        name设置为None时，全部完成。（这还有意义吗。。）
        """
        self._set_users(name, 0)

    def _init_status(self):
        """
        初始化运行状态self.run_status
        self.run_status={
            rec_addr : state
        }
        其中rec_addr为存档路径，state为：
            0： 未执行 <- 初始状态
            1： 已完成
            2： 已跳过
        """
        for _, _, _, _, rec in self.SL:
            self.run_status[rec] = 0
            self.checked_status[rec] = False

    def _get_status(self):
        """
        获取保存的进度，写入self.run_status
        """
        obj = self._load()
        obj.setdefault("run_status", {})
        for i, j in obj["run_status"].items():
            self.run_status[i] = j

    def _get_last_time(self):
        obj = self._load()
        obj.setdefault("last_time", 0)
        return obj["last_time"]

    def _set_status(self):
        """
        写入当前的进度
        """
        obj = self._load()
        obj["run_status"] = self.run_status
        self._save(obj)

    def _set_time(self, time):
        obj = self._load()
        obj["last_time"] = time
        self._save(obj)

    def _add(self, name, batch):
        """
        将一个schedule添加到PCR中
        运行路径：
        rec/<schedules_name>/<schedule_name>/<batch_name>
        """
        rec_addr = os.path.join("rec", self.name, name, batch)
        os.makedirs(rec_addr, exist_ok=True)
        parsed = parse_batch(AutomatorRecorder.getbatch(batch))
        self.pcr.add_tasks(parsed, True, rec_addr)

    def log(self, level, content):
        """
        生成log文件在log/schedule_<schedule_name>.txt中
        """
        pcr_log(f"schedule_{self.name}").write_log(level, content)

    @staticmethod
    def is_complete(rec):
        """
        判断记录Rec是否已经全部完成
        :param rec: 存档目录
        """
        if os.path.exists(os.path.join(rec, "_fin")):
            return True
        _, bat = os.path.split(rec)
        parsed = parse_batch(AutomatorRecorder.getbatch(bat))
        for _, acc, _ in parsed:
            rs = AutomatorRecorder(acc, rec).get_run_status()
            if not rs["finished"] or rs["error"] is not None:
                return False
        with open(os.path.join(rec, "_fin"), "w") as f:
            f.write("出现这个文件表示该文件夹内的记录已经刷完。")
        return True

    @staticmethod
    def count_complete(rec):
        """
        统计记录Rec中完成账号的数量
        输出：完成数 / 总数
        """
        _, bat = os.path.split(rec)
        parsed = parse_batch(AutomatorRecorder.getbatch(bat))
        L = len(parsed)
        if os.path.exists(os.path.join(rec, "_fin")):
            return L, L
        else:
            cnt = 0
            for _, acc, _ in parsed:
                rs = AutomatorRecorder(acc, rec).get_run_status()
                if rs["finished"] and rs["error"] is None:
                    cnt += 1
            return cnt, L

    def is_free(self):
        """
        判断是否闲置
        """
        if self.pcr.devices.count() > 0:
            return False
        if len(self.pcr.tasks.get_attribute("queue")) > 0:
            return False
        return True

    def _run(self):
        self._get_status()
        _time_start = time.time()  # 第一次直接输出初始状态
        if len(s_sckey) != 0:
            acc_state = f"Schedule {self.name} 开始运行！\n"
            from CreateUser import _show_schedule
            acc_state += PrintToStr(_show_schedule, self.schedule)
            acc_state += PrintToStr(self.show_device)
            acc_state += PrintToStr(self.show_schedule)
            pcr_log("admin").server_bot("STATE", acc_state=acc_state)

        while self.state == 1:
            # PCRInitializer information
            while True:
                p = self.pcr.get_log()
                if p is None:
                    break
                self.log("info", p)

            # Report Information
            if not self.is_free() and len(s_sckey) != 0:
                _time_end = time.time()
                _time = int(_time_end - _time_start) / 60
                if _time >= s_sentstate:
                    self.log("info", "server_bot 播报当前状态")
                    pcr_log("admin").server_bot("STATE", acc_state=PrintToStr(self.show_everything))
                    _time_start = time.time()

            if "restart" in self.config:
                last_time = self._get_last_time()
                cur_time = time.time()
                # flag: 一个是否需要restart的标记
                if last_time == 0 or diffday(cur_time, last_time, self.config["restart"]):
                    self.log("info", "Config-清除全部运行记录")
                    self.restart()
                    self._set_time(cur_time)

            for ind, t5 in enumerate(self.SL):
                typ, nam, bat, cond, rec = t5
                # 已经完成、跳过
                if self.run_status[rec] != 0 and (nam, bat) in self.always_restart_name:
                    self.log("info", f"计划 {nam} 重置。")
                    self.restart(nam)
                if self.run_status[rec] != 0:
                    continue
                # 检查是否已经完成
                if self.is_complete(rec):
                    # 记录设置2：运行完成后立刻restart
                    self.run_status[rec] = 1
                    self.log("info", f"计划** {nam} - {bat} **已经完成")
                    self._set_status()
                # 已经处理过
                if self.checked_status[rec] is True:
                    continue
                if self._check(cond):
                    # 满足条件
                    self.checked_status[rec] = True
                    self._add(nam, bat)
                    self.log("info", f"开始执行计划：** {nam} - {bat} **")
                else:
                    if typ == "asap":
                        self.run_status[rec] = 2
                        self.log("info", f"跳过计划：** {nam} **")
            time.sleep(1)

    def run(self):
        """
        开启新线程，执行存储在self.SL中的逻辑
        """
        if self.state == 0:
            self.state = 1
            self._init_status()
            self.run_thread = threading.Thread(target=Schedule._run, args=(self,), daemon=True).start()
            self.log("info", "Schedule线程启动！")
        else:
            self.log("info", "Schedule线程已经启动了。")

    @staticmethod
    def _check(cond: dict) -> bool:
        """
        检查某个计划是否满足全部condition
        """
        tm = time.time()
        st = time.localtime(tm)
        if "start_hour" in cond:
            # 时间段条件
            sh = cond["start_hour"]
            eh = cond["end_hour"]
            ch = st.tm_hour
            if sh <= eh:
                flag = sh <= ch <= eh
            else:
                flag = (0 <= ch <= eh) or (sh <= ch <= 23)
            if not flag:
                return False
        if "can_juanzeng" in cond:
            # 可以捐赠条件
            AR = AutomatorRecorder(cond["can_juanzeng"], None)
            ts = AR.get("time_status", UDD["time_status"])
            tm = ts["juanzeng"]
            diff = time.time() - tm
            if diff < 8 * 3600 + 60:
                return False
        if "_last_rec" in cond:
            # 前置batch条件
            if not Schedule.is_complete(cond["_last_rec"]):
                return False
        return True

    def run_first_time(self):
        """
        清除全部记录并运行
        """
        self.restart()
        self.run()

    def run_continue(self):
        """
        从上次进度继续运行
        """
        self.run()

    def stop(self):
        """
        停止Schedule运行。
        清空PCR中剩下未完成的任务队列，并且等待当前执行完毕。
        """
        self.state = 0
        self.log("info", "停止中，已清空任务队列，等待当前任务执行完毕")
        self.pcr.stop(True, True)
        self.log("info", "Schedule已经停止。")

    def join(self):
        """
        一直运行直到队列全部任务运行完毕
        """
        while True:
            if "restart" in self.config:
                time.sleep(1000)
                continue
            for i in self.run_status:
                if self.run_status[i] == 0:
                    break
            else:
                break
            time.sleep(1)

    def get_rec_status(self, rec):
        """
        获取某一个记录目录下的任务运行情况
        """
        if self.is_complete(rec):
            return "执行完毕"
        elif self.run_status[rec] == 2:
            return "跳过"
        elif self.checked_status[rec]:
            cnt, tot = self.count_complete(rec)
            return f"执行中： {cnt} / {tot}"
        else:
            return "等待执行"

    def get_status(self, last_state=False):
        """
        获取当前计划执行状态
        last_state：不是获得当前状态，而是获取上次状态
        其中，每个rec的状态包括：
        status：状态
            wait 等待执行
            skip 跳过
            busy 正在执行
            fin  完成
            err  错误
            last 上次
        """
        L = []
        for i, j in self.subs.items():
            D = {}
            D["name"] = i
            if type(j) is tuple:
                D["mode"] = "batch"
                bat, rec = j
                if self.is_complete(rec):
                    D["status"] = "fin"
                elif not last_state and self.run_status[rec] == 2:
                    D["status"] = "skip"  # 跳过
                elif last_state or self.checked_status[rec]:
                    if last_state:
                        D["status"] = "last"
                    else:
                        D["status"] = "busy"  # 正在执行
                    D["detail"] = AutomatorRecorder.get_batch_state(bat, rec)
                    D["error"] = {}
                    D["cnt"] = D["detail"]["error"] + D["detail"]["finish"]
                    D["tot"] = D["detail"]["total"]
                    if D["detail"]["error"] != 0:
                        D["status"] = "err"
                        # 统计出错用户
                        for _acc, _c in D["detail"]["detail"].items():
                            if _c["error"] is not None:
                                D["error"][_acc] = _c
                else:
                    D["status"] = "wait"  # 等待执行
            else:
                tot = len(j)
                cnt = 0
                D["mode"] = "batches"
                D["status"] = "wait"
                for bat, rec in j:
                    if not last_state:
                        if self.is_complete(rec) == 1:
                            cnt += 1
                            continue
                        elif self.run_status[rec] == 2:
                            D["status"] = "skip"
                        elif self.checked_status[rec]:
                            D["status"] = "busy"
                            D["current"] = bat
                            D["detail"] = AutomatorRecorder.get_batch_state(bat, rec)
                            D["error"] = {}
                            D["cnt"] = D["detail"]["error"] + D["detail"]["finish"]
                            D["tot"] = D["detail"]["total"]
                            if D["detail"]["error"] != 0:
                                D["status"] = "err"
                                # 统计出错用户
                                for _acc, _c in D["detail"]["detail"].items():
                                    if _c["error"] is not None:
                                        D["error"][_acc] = _c
                        else:
                            break
                        if D["status"] != "busy":
                            break
                    else:
                        D["status"] = "last"
                        if self.is_complete(rec):
                            cnt += 1
                            if cnt == tot:
                                D["status"] = "fin"
                            continue
                        else:
                            D["current"] = bat
                            D["detail"] = AutomatorRecorder.get_batch_state(bat, rec)
                            D["error"] = {}
                            D["cnt"] = D["detail"]["error"] + D["detail"]["finish"]
                            D["tot"] = D["detail"]["total"]
                            if D["detail"]["error"] != 0:
                                D["status"] = "err"
                                # 统计出错用户
                                for _acc, _c in D["detail"]["detail"].items():
                                    if _c["error"] is not None:
                                        D["error"][_acc] = _c
                            break
                D["batch_fin"] = cnt
                D["batch_tot"] = tot
            L += [D]
        return L

    def show_schedule(self, last_state=False):
        """
        展示当前计划执行情况
        """
        status = self.get_status(last_state)
        print("= 执行进度 =")
        for D in status:
            if D["mode"] == "batch":
                print(f"** {D['name']} ** ", end="")
                if D["status"] == "wait":
                    print("等待执行")
                elif D["status"] == "fin":
                    print("执行完毕")
                elif D["status"] == "skip":
                    print("跳过")
                else:
                    if D["cnt"] < D["tot"]:
                        print("进行中 进度：", D["cnt"], "/", D["tot"])
                    if len(D["error"]) > 0:
                        print("+ 存在未解决的错误")
                        DEL = [(_a, _b["state_str"]) for _a, _b in D["error"].items()]
                        for _acc, _err in DEL:
                            print("+ ", _acc, ":", _err)
            elif D["mode"] == "batches":
                print(f"** {D['name']} ** ", end="")
                if D["status"] == "wait":
                    print("等待执行")
                elif D["status"] == "fin":
                    print("执行完毕")
                elif D["status"] == "skip":
                    print("跳过")
                else:
                    print("批次：", D["batch_fin"], "/", D["batch_tot"])
                    print("+ 当前批次：", D["current"], end=" ")
                    if D["cnt"] < D["tot"]:
                        print("进行中 进度：", D["cnt"], "/", D["tot"])
                    if len(D["error"]) > 0:
                        print("+ 存在未解决的错误")
                        DEL = [(_a, _b["state_str"]) for _a, _b in D["error"].items()]
                        for _acc, _err in DEL:
                            print("+ ", _acc, ":", _err)

    def show_queue(self):
        """
        显示任务队列
        """
        self.pcr.show()

    def show_device(self):
        """
        显示设备情况
        """
        self.pcr.devices.show()

    def show_everything(self):
        self.show_schedule()
        self.show_queue()
        self.show_device()
